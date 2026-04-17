"""
VTES Card Scanner - build_index.py
Run this script ONCE to index all images.
It generates 'card_index.pkl', which the server uses for matching.

Usage:
    python build_index.py --cards "/path/to/img/cards"
    python build_index.py --cards "/path/to/cards.zip"
    python build_index.py --cards "https://example.com/cards.zip"
"""

import os
import pickle
import argparse
import time
import cv2
import numpy as np
import urllib.request
import zipfile
import tempfile
import shutil

def build_index(cards_root: str, output: str = "card_index.pkl"):
    orb = cv2.ORB_create(nfeatures=300)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    index = []  # List of dicts: {id, set, path, keypoints, descriptors}

    sets_dir = os.path.join(cards_root, "sets")
    total = 0
    failed = 0

    print(f"\n📁 Indexing from: {cards_root}")
    print("=" * 60)

    # --- Phase 1: images by set (high priority) ---
    if os.path.isdir(sets_dir):
        set_names = sorted(os.listdir(sets_dir))
        print(f"Sets found: {len(set_names)}")

        for set_name in set_names:
            set_path = os.path.join(sets_dir, set_name)
            if not os.path.isdir(set_path):
                continue

            files = [f for f in os.listdir(set_path) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]
            print(f"  [{set_name}] {len(files)} cards", end="", flush=True)
            set_ok = 0

            for fname in files:
                card_id = os.path.splitext(fname)[0]
                fpath = os.path.join(set_path, fname)

                img = cv2.imread(fpath, cv2.IMREAD_GRAYSCALE)
                if img is None:
                    failed += 1
                    continue

                img = cv2.resize(img, (300, 420))
                img = clahe.apply(img)
                kp, des = orb.detectAndCompute(img, None)
                if des is None or len(kp) < 10:
                    failed += 1
                    continue

                index.append({
                    "id": card_id,
                    "set": set_name,
                    "path": fpath,
                    "keypoints": np.float32([k.pt for k in kp]),
                    "descriptors": des,
                })
                set_ok += 1
                total += 1

            print(f" → {set_ok} indexed")
    else:
        print("⚠️  'sets/' folder not found")

    # --- Phase 2: root images (fallback if they are not under sets) ---
    indexed_ids_in_sets = {e["id"] for e in index}
    root_files = [f for f in os.listdir(cards_root)
                  if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))
                  and os.path.isfile(os.path.join(cards_root, f))]

    root_new = 0
    for fname in root_files:
        card_id = os.path.splitext(fname)[0]
        if card_id in indexed_ids_in_sets:
            continue  # Already indexed with a set

        fpath = os.path.join(cards_root, fname)
        img = cv2.imread(fpath, cv2.IMREAD_GRAYSCALE)
        if img is None:
            failed += 1
            continue

        img = cv2.resize(img, (300, 420))
        img = clahe.apply(img)
        kp, des = orb.detectAndCompute(img, None)
        if des is None or len(kp) < 10:
            failed += 1
            continue

        index.append({
            "id": card_id,
            "set": None,  # Unknown set
            "path": fpath,
            "keypoints": np.float32([k.pt for k in kp]),
            "descriptors": des,
        })
        root_new += 1
        total += 1

    print(f"\n  [root] {root_new} additional cards (unknown set fallback)")

    # --- Save index ---
    print(f"\n💾 Saving index ({total} cards)...")
    with open(output, "wb") as f:
        pickle.dump(index, f, protocol=pickle.HIGHEST_PROTOCOL)

    size_mb = os.path.getsize(output) / (1024 * 1024)
    print(f"✅ Index saved to '{output}' ({size_mb:.1f} MB)")
    print(f"   Total indexed   : {total}")
    print(f"   Failed/skipped  : {failed}")
    print("\nYou can now run: python server.py")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VTES - Build card index")
    default_cards = os.path.join(os.path.dirname(__file__), "img", "cards")
    parser.add_argument(
        "--cards",
        default=default_cards,
        help="Path/zip/URL to the root folder containing card images (dir, .zip, or http(s) .zip)"
    )
    parser.add_argument(
        "--output",
        default="card_index.pkl",
        help="Index file name (default: card_index.pkl)"
    )
    args = parser.parse_args()

    def is_url(path: str) -> bool:
        return path.startswith("http://") or path.startswith("https://")

    def download_and_extract_zip(url: str) -> str:
        tmp_dir = tempfile.mkdtemp(prefix="vtes_cards_")
        tmp_zip = os.path.join(tmp_dir, "cards.zip")
        print(f"⬇️  Downloading ZIP from: {url}")
        try:
            urllib.request.urlretrieve(url, tmp_zip)
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        print(f"📦 Extracting to: {tmp_dir}")
        with zipfile.ZipFile(tmp_zip, "r") as z:
            z.extractall(tmp_dir)
        return tmp_dir

    def extract_local_zip(zip_path: str) -> str:
        tmp_dir = tempfile.mkdtemp(prefix="vtes_cards_")
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(tmp_dir)
        return tmp_dir

    cards_arg = args.cards
    cards_root = None

    try:
        if is_url(cards_arg):
            cards_root = download_and_extract_zip(cards_arg)
        elif os.path.isfile(cards_arg) and cards_arg.lower().endswith(".zip"):
            cards_root = extract_local_zip(cards_arg)
        else:
            cards_root = cards_arg

        if not os.path.isdir(cards_root):
            print(f"❌ Folder not found: {cards_root}")
            print("   Provide a directory, a .zip file, or a http(s) .zip URL with --cards")
            exit(1)

        t0 = time.time()
        build_index(cards_root, args.output)
        print(f"\n⏱  Total time: {time.time() - t0:.1f}s")
    finally:
        try:
            if cards_root and cards_root.startswith(tempfile.gettempdir()) and (
                is_url(cards_arg) or (os.path.isfile(cards_arg) and cards_arg.lower().endswith(".zip"))
            ):
                shutil.rmtree(cards_root, ignore_errors=True)
        except Exception:
            pass