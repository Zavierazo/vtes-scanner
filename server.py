"""
VTES Card Scanner - server.py
A Flask server that receives a camera image and returns the card ID + set.

Usage:
    python server.py
    Then open http://localhost:5000 in your browser.
"""

import os
import pickle
import base64
import time
import cv2
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder=".")

INDEX_FILE = "card_index.pkl"
INDEX = []
ORB = None
BF_MATCHER = None
CLAHE = None
PHASE1_POOL = None
MIN_ALT_CONF = 20  # Minimum confidence (%) for alternatives to be included in results

def load_index():
    global INDEX, ORB, BF_MATCHER, CLAHE, PHASE1_POOL
    if not os.path.exists(INDEX_FILE):
        print(f"❌ '{INDEX_FILE}' not found.")
        print("   Run first: python build_index.py")
        return False

    print(f"📦 Loading index from '{INDEX_FILE}'...")
    t0 = time.time()
    with open(INDEX_FILE, "rb") as f:
        INDEX = pickle.load(f)

    ORB = cv2.ORB_create(nfeatures=300)
    BF_MATCHER = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    CLAHE = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    # Persistent pool for Phase 1. Worker count = logical CPU count so all cores
    # are used; OpenCV releases the GIL inside knnMatch so threads run in parallel.
    PHASE1_POOL = ThreadPoolExecutor(max_workers=os.cpu_count())

    print(f"✅ Index loaded: {len(INDEX)} cards in {time.time()-t0:.1f}s")
    return True


def match_card(img_gray: np.ndarray, top_k: int = 10, id_only: bool = False, no_alternatives: bool = False):
    """
    Two-phase matching:
      1. Fast ratio-test filter over the full index.
      2. Homography/RANSAC inlier check on the top candidates (requires keypoints in index).
    Returns the best match and up to top_k-1 alternatives.
    When no_alternatives=True only the best candidate is needed, so Phase 2 runs
    on a much smaller window (top-5 instead of top-40) for a significant speed-up.
    """
    img_resized = cv2.resize(img_gray, (300, 420))

    # Normalize contrast (same preprocessing as build_index)
    img_resized = CLAHE.apply(img_resized)

    kp_query, des_query = ORB.detectAndCompute(img_resized, None)
    if des_query is None or len(kp_query) < 10:
        return None, []

    # Phase 1: parallel per-card ratio-test filter.
    # knnMatch releases the Python GIL so threads run genuinely in parallel.
    # _score_entry is a module-level function (required for ThreadPoolExecutor).
    LOWE_RATIO = 0.72

    def _score_entry(entry):
        des_train = entry["descriptors"]
        if des_train is None or len(des_train) < 10:
            return None
        matches = BF_MATCHER.knnMatch(des_query, des_train, k=2)
        good = [
            m for m_pair in matches
            if len(m_pair) == 2
            for m, n in [m_pair]
            if m.distance < LOWE_RATIO * n.distance
        ]
        return (len(good), entry, good) if len(good) >= 4 else None

    raw = [r for r in PHASE1_POOL.map(_score_entry, INDEX) if r is not None]

    if not raw:
        return None, []

    raw.sort(key=lambda x: x[0], reverse=True)

    # Phase 2: homography verification on top candidates (needs keypoints in index).
    # When no_alternatives is True we only need the winner, so limit to top-5 to
    # avoid running the expensive RANSAC step on low-probability candidates.
    phase2_window = 5 if no_alternatives else 40
    has_keypoints = "keypoints" in raw[0][1]
    if has_keypoints:
        verified = []
        for _, entry, good_matches in raw[:phase2_window]:
            kp_train = entry["keypoints"]  # (N, 2) float32

            src_pts = np.float32(
                [kp_query[m.queryIdx].pt for m in good_matches]
            ).reshape(-1, 1, 2)
            dst_pts = np.float32(
                [kp_train[m.trainIdx] for m in good_matches]
            ).reshape(-1, 1, 2)

            _, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
            if mask is None:
                continue
            inliers = int(mask.ravel().sum())
            if inliers >= 4:
                verified.append((inliers, entry))

        verified.sort(key=lambda x: x[0], reverse=True)
        scores = verified
        # Absolute scale: 20 inliers → 100%. Anything above is still capped at 100%.
        conf_scale = 5
    else:
        # Legacy index without keypoints: fall back to raw match count
        scores = [(c, e) for c, e, _ in raw]
        # Absolute scale: 50 raw matches → 100%.
        conf_scale = 2

    if not scores or scores[0][0] < 4:
        return None, []

    # When id_only, merge scores by card id (keep max score per id)
    if id_only:
        id_best_score: dict = {}
        id_best_entry: dict = {}
        for score, entry in scores:
            cid = entry["id"]
            if cid not in id_best_score or score > id_best_score[cid]:
                id_best_score[cid] = score
                id_best_entry[cid] = entry
        scores = sorted(
            [(id_best_score[cid], id_best_entry[cid]) for cid in id_best_score],
            key=lambda x: x[0],
            reverse=True,
        )

    best_score, best_entry = scores[0]

    # Absolute confidence: score × scale, capped at 100%.
    # Both main result and alternatives use the same scale so the values are
    # honestly comparable — a weak best match will show e.g. 45%, not 100%.
    def score_to_conf(s):
        return min(100, int(round(s * conf_scale)))

    result = {
        "id": best_entry["id"],
        "set": None if id_only else best_entry["set"],
        "score": best_score,
        "confidence": score_to_conf(best_score),
        "path": best_entry["path"],
    }

    # Skip building alternatives when not needed.
    if no_alternatives:
        return result, []

    # Deduplicate alternatives by (id, set) or id-only depending on mode.
    seen_ids = {best_entry["id"]}
    seen_pairs = {(best_entry["id"], best_entry["set"])}
    alternatives = []
    for score, entry in scores[1:]:
        if id_only:
            if entry["id"] in seen_ids:
                continue
            seen_ids.add(entry["id"])
            alt = {"id": entry["id"], "score": score}
        else:
            key = (entry["id"], entry["set"])
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            alt = {"id": entry["id"], "set": entry["set"], "score": score}
        alt_conf = score_to_conf(score)
        if alt_conf < MIN_ALT_CONF:
            continue
        alt["confidence"] = alt_conf
        alternatives.append(alt)
        if len(alternatives) >= top_k - 1:
            break

    return result, alternatives


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/scan", methods=["POST"])
def scan():
    data = request.get_json()
    if not data or "image" not in data:
        return jsonify({"error": "No image provided"}), 400

    if not INDEX:
        return jsonify({"error": "Index not loaded. Run build_index.py first."}), 500

    # Decode base64 image
    try:
        img_data = base64.b64decode(data["image"])
        img_array = np.frombuffer(img_data, dtype=np.uint8)
        img = cv2.imdecode(img_array, cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise ValueError("Could not decode image")
    except Exception as e:
        return jsonify({"error": f"Error decoding image: {str(e)}"}), 400

    id_only = bool(data.get("id_only", False))
    no_alternatives = bool(data.get("no_alternatives", False))

    t0 = time.time()
    result, alternatives = match_card(img, id_only=id_only, no_alternatives=no_alternatives)
    elapsed = round((time.time() - t0) * 1000)

    if result is None:
        return jsonify({
            "found": False,
            "message": "Not enough features were detected. Improve the lighting or move the card closer.",
            "elapsed_ms": elapsed,
        })

    # Confidence level as text
    conf = result["confidence"]
    if conf >= 60:
        conf_label = "high"
    elif conf >= 35:
        conf_label = "medium"
    else:
        conf_label = "low"

    response = {
        "found": True,
        "id": result["id"],
        "confidence": conf,
        "confidence_label": conf_label,
        "score": result["score"],
        "elapsed_ms": elapsed,
    }
    if not id_only:
        response["set"] = result["set"] or "unknown"
    if not no_alternatives:
        response["alternatives"] = alternatives
    return jsonify(response)


@app.route("/status")
def status():
    return jsonify({
        "ready": len(INDEX) > 0,
        "cards_indexed": len(INDEX),
        "index_file": INDEX_FILE,
    })


if __name__ == "__main__":
    ok = load_index()
    if not ok:
        print("\n⚠️  The server will start, but matching will not work.")
        print("   Run 'python build_index.py' and restart the server.\n")

    print("\n🌐 Server available at: http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)