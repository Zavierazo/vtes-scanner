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
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder=".")

INDEX_FILE = "card_index.pkl"
INDEX = []
ORB = None
BF_MATCHER = None

def load_index():
    global INDEX, ORB, BF_MATCHER
    if not os.path.exists(INDEX_FILE):
        print(f"❌ '{INDEX_FILE}' not found.")
        print("   Run first: python build_index.py")
        return False

    print(f"📦 Loading index from '{INDEX_FILE}'...")
    t0 = time.time()
    with open(INDEX_FILE, "rb") as f:
        INDEX = pickle.load(f)

    ORB = cv2.ORB_create(nfeatures=1000)
    BF_MATCHER = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)

    print(f"✅ Index loaded: {len(INDEX)} cards in {time.time()-t0:.1f}s")
    return True


def match_card(img_gray: np.ndarray, top_k: int = 8):
    """
    Two-phase matching:
      1. Fast ratio-test filter over the full index.
      2. Homography/RANSAC inlier check on the top candidates (requires keypoints in index).
    Returns the best match and up to top_k-1 alternatives.
    """
    img_resized = cv2.resize(img_gray, (300, 420))

    # Normalize contrast (same preprocessing as build_index)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    img_resized = clahe.apply(img_resized)

    kp_query, des_query = ORB.detectAndCompute(img_resized, None)
    if des_query is None or len(kp_query) < 10:
        return None, []

    # Phase 1: collect raw ratio-test scores for every entry
    LOWE_RATIO = 0.72
    raw = []
    for entry in INDEX:
        des_train = entry["descriptors"]
        if des_train is None or len(des_train) < 10:
            continue

        matches = BF_MATCHER.knnMatch(des_query, des_train, k=2)
        good = []
        for m_pair in matches:
            if len(m_pair) == 2:
                m, n = m_pair
                if m.distance < LOWE_RATIO * n.distance:
                    good.append(m)

        if len(good) >= 4:
            raw.append((len(good), entry, good))

    if not raw:
        return None, []

    raw.sort(key=lambda x: x[0], reverse=True)

    # Phase 2: homography verification on top-30 candidates (needs keypoints in index).
    # Using 30 so that set-variants of the same card (near-identical art) are all captured.
    has_keypoints = "keypoints" in raw[0][1]
    if has_keypoints:
        verified = []
        for _, entry, good_matches in raw[:30]:
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

    best_score, best_entry = scores[0]

    # Absolute confidence: score × scale, capped at 100%.
    # Both main result and alternatives use the same scale so the values are
    # honestly comparable — a weak best match will show e.g. 45%, not 100%.
    def score_to_conf(s):
        return min(100, int(round(s * conf_scale)))

    result = {
        "id": best_entry["id"],
        "set": best_entry["set"],
        "score": best_score,
        "confidence": score_to_conf(best_score),
        "path": best_entry["path"],
    }

    # Include all alternatives including same card in different sets.
    # Deduplicate by (id, set) pair — keep the highest-scoring entry per pair.
    seen = {(best_entry["id"], best_entry["set"])}
    alternatives = []
    for score, entry in scores[1:]:
        key = (entry["id"], entry["set"])
        if key in seen:
            continue
        seen.add(key)
        alt_conf = score_to_conf(score)
        alternatives.append({
            "id": entry["id"],
            "set": entry["set"],
            "score": score,
            "confidence": alt_conf,
        })
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

    t0 = time.time()
    result, alternatives = match_card(img)
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

    return jsonify({
        "found": True,
        "id": result["id"],
        "set": result["set"] or "unknown",
        "confidence": conf,
        "confidence_label": conf_label,
        "score": result["score"],
        "elapsed_ms": elapsed,
        "alternatives": alternatives[:3],
    })


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