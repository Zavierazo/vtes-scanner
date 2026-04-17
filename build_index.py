"""
VTES Card Scanner - build_index.py
Run this script ONCE to index all images.
It generates 'card_index.pkl', which the server uses for matching.

Usage:
    python build_index.py --cards "D:/Trabajo/Git/vtesdecks-statics/public/img/cards"
"""

import os
import pickle
import argparse
import time
import cv2
import numpy as np

def build_index(cards_root: str, output: str = "card_index.pkl"):
    orb = cv2.ORB_create(nfeatures=1000)
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
    parser.add_argument(
        "--cards",
        default=r"D:\Trabajo\Git\vtesdecks-statics\public\img\cards",
        help="Path to the root folder containing card images"
    )
    parser.add_argument(
        "--output",
        default="card_index.pkl",
        help="Index file name (default: card_index.pkl)"
    )
    args = parser.parse_args()

    if not os.path.isdir(args.cards):
        print(f"❌ Folder not found: {args.cards}")
        print("   Use --cards to specify the correct path")
        exit(1)

    t0 = time.time()
    build_index(args.cards, args.output)
    print(f"\n⏱  Total time: {time.time() - t0:.1f}s")