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
import logging
import json
import threading
from contextlib import contextmanager
import cv2
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, send_from_directory, g

app = Flask(__name__, static_folder=".")

# Use gunicorn's logger when running under gunicorn, fall back to a basic
# stderr logger for direct `python server.py` runs.
gunicorn_logger = logging.getLogger("gunicorn.error")
if (gunicorn_logger.handlers):
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
else:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    app.logger.setLevel(logging.INFO)

def positive_env(name, default):
    value = int(os.environ.get(name, default))
    if (value < 1):
        raise ValueError(f"{name} must be a positive integer")
    return value


INDEX_FILE = os.environ.get("INDEX_FILE", "card_index.pkl")
MATCH_WORKERS = os.cpu_count() or 1
SEARCH_MODE = os.environ.get("SEARCH_MODE", "exact")
if (SEARCH_MODE not in ("exact", "lsh")):
    raise ValueError("SEARCH_MODE must be exact or lsh")
LSH_CANDIDATES = positive_env("LSH_CANDIDATES", 100)
TIMING_LOGS = os.environ.get("TIMING_LOGS", "0") == "1"
INDEX = []
ORB = None
BF_MATCHER = None
CLAHE = None
PHASE1_POOL = None
RETRIEVER = None
MATCH_LOCK = threading.RLock()
MIN_ALT_CONF = 20  # Minimum confidence (%) for alternatives to be included in results


@contextmanager
def timed(metrics, stage):
    if (metrics is None):
        yield
        return
    wall, cpu = time.perf_counter(), time.process_time()
    try:
        yield
    finally:
        values = metrics.setdefault(stage, {"wallMs": 0.0, "cpuMs": 0.0})
        values["wallMs"] += (time.perf_counter() - wall) * 1000
        values["cpuMs"] += (time.process_time() - cpu) * 1000


class CandidateRetriever:
    """Experimental binary-descriptor lookup; exact scoring remains per image."""

    def __init__(self, entries):
        valid = [(i, e["descriptors"]) for i, e in enumerate(entries)
                 if (e["descriptors"] is not None and len(e["descriptors"]) >= 10)]
        if (not valid):
            raise ValueError("No descriptors available for LSH")
        self.descriptors = np.ascontiguousarray(np.concatenate([d for _, d in valid]))
        self.owners = np.repeat(np.array([i for i, _ in valid], dtype=np.int32),
                                [len(d) for _, d in valid])
        self.entries = entries
        self.by_id = {}
        for i, entry in enumerate(entries):
            self.by_id.setdefault(entry["id"], []).append(i)
        # A binary LSH index, not a float/KD-tree approximation of Hamming distance.
        self.index = cv2.flann_Index(self.descriptors, {
            "algorithm": 6, "table_number": 6, "key_size": 20,
            "multi_probe_level": 1,
        })

    def shortlist(self, descriptors, count):
        neighbors, _ = self.index.knnSearch(descriptors, 8, params={})
        votes = np.zeros(len(self.entries), dtype=np.int32)
        for row in neighbors:
            valid = row[(row >= 0) & (row < len(self.owners))]
            # One vote per query descriptor per image, even with repeated features.
            votes[np.unique(self.owners[valid])] += 1
        ranked = sorted(np.flatnonzero(votes), key=lambda i: (-int(votes[i]), int(i)))[:count]
        selected = set()
        for i in ranked:
            selected.update(self.by_id[self.entries[i]["id"]])
        # Preserve original order so exact-score ties retain baseline behavior.
        return [self.entries[i] for i in sorted(selected)]


def load_index():
    global INDEX, ORB, BF_MATCHER, CLAHE, PHASE1_POOL, RETRIEVER
    if (not os.path.exists(INDEX_FILE)):
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
    if (PHASE1_POOL is not None):
        PHASE1_POOL.shutdown()
    PHASE1_POOL = ThreadPoolExecutor(max_workers=MATCH_WORKERS)
    RETRIEVER = None
    if (SEARCH_MODE == "lsh"):
        try:
            RETRIEVER = CandidateRetriever(INDEX)
        except (cv2.error, MemoryError, ValueError):
            app.logger.exception("LSH initialization failed; using exhaustive search")

    print(f"✅ Index loaded: {len(INDEX)} cards in {time.time()-t0:.1f}s")
    app.logger.info("Matcher ready | requested=%s active=%s match_threads=%d opencv_threads=%d",
                    SEARCH_MODE, "lsh" if (RETRIEVER is not None) else "exact",
                    MATCH_WORKERS, cv2.getNumThreads())
    return True


def match_card(img_gray: np.ndarray, top_k: int = 10, id_only: bool = False,
               no_alternatives: bool = False, metrics=None):
    # Flask's development server can accept concurrent requests. Serialize access
    # to mutable OpenCV resources; Gunicorn uses synchronous worker processes.
    with MATCH_LOCK, timed(metrics, "matching"):
        with timed(metrics, "preprocessing"):
            img_resized = CLAHE.apply(cv2.resize(img_gray, (300, 420)))
            kp_query, des_query = ORB.detectAndCompute(img_resized, None)
        if (des_query is None or len(kp_query) < 10):
            return None, []
        entries = INDEX
        if (RETRIEVER is not None):
            try:
                with timed(metrics, "retrieval"):
                    entries = RETRIEVER.shortlist(des_query, LSH_CANDIDATES)
                if (not entries):
                    entries = INDEX
            except (cv2.error, MemoryError, ValueError):
                app.logger.exception("LSH retrieval failed; using exhaustive search")
                entries = INDEX
        if (metrics is not None):
            metrics["candidates"] = len(entries)
            metrics["fallback"] = SEARCH_MODE == "lsh" and entries is INDEX
        result = _match_candidates(kp_query, des_query, entries, top_k, id_only,
                                   no_alternatives, metrics)
        if (result[0] is None and entries is not INDEX and len(entries) < len(INDEX)):
            if (metrics is not None):
                metrics["fallback"] = True
            return _match_candidates(kp_query, des_query, INDEX, top_k, id_only,
                                     no_alternatives, metrics)
        return result


def _match_candidates(kp_query, des_query, entries, top_k, id_only, no_alternatives, metrics):
    """
    Two-phase matching:
      1. Fast ratio-test filter over the full index.
      2. Homography/RANSAC inlier check on the top candidates (requires keypoints in index).
    Returns the best match and up to top_k-1 alternatives.
    When no_alternatives=True only the best candidate is needed, so Phase 2 runs
    on a much smaller window (top-5 instead of top-40) for a significant speed-up.
    """
    # Phase 1: same per-image ratio test for exhaustive and shortlisted search.
    LOWE_RATIO = 0.72

    def _score_entry(entry):
        des_train = entry["descriptors"]
        if (des_train is None or len(des_train) < 10):
            return None
        matches = BF_MATCHER.knnMatch(des_query, des_train, k=2)
        good = [
            m for m_pair in matches
            if len(m_pair) == 2
            for m, n in [m_pair]
            if m.distance < LOWE_RATIO * n.distance
        ]
        return (len(good), entry, good) if len(good) >= 4 else None

    with timed(metrics, "descriptorSearch"):
        raw = [r for r in PHASE1_POOL.map(_score_entry, entries) if r is not None]

    if (not raw):
        return None, []

    raw.sort(key=lambda x: x[0], reverse=True)

    # Phase 2: homography verification on top candidates (needs keypoints in index).
    # When no_alternatives is True we only need the winner, so limit to top-5 to
    # avoid running the expensive RANSAC step on low-probability candidates.
    phase2_window = 5 if no_alternatives else 40
    has_keypoints = "keypoints" in raw[0][1]
    if (has_keypoints):
        verified = []
        for _, entry, good_matches in raw[:phase2_window]:
            kp_train = entry["keypoints"]  # (N, 2) float32

            src_pts = np.float32(
                [kp_query[m.queryIdx].pt for m in good_matches]
            ).reshape(-1, 1, 2)
            dst_pts = np.float32(
                [kp_train[m.trainIdx] for m in good_matches]
            ).reshape(-1, 1, 2)

            with timed(metrics, "homography"):
                _, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
            if (mask is None):
                continue
            inliers = int(mask.ravel().sum())
            if (inliers >= 4):
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

    if (not scores or scores[0][0] < 4):
        return None, []

    # When id_only, merge scores by card id (keep max score per id)
    if (id_only):
        id_best_score: dict = {}
        id_best_entry: dict = {}
        for score, entry in scores:
            cid = entry["id"]
            if (cid not in id_best_score or score > id_best_score[cid]):
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
        "id": int(best_entry["id"]),
        "set": None if id_only else best_entry["set"],
        "score": best_score,
        "confidence": score_to_conf(best_score),
        "path": best_entry["path"],
    }

    # Skip building alternatives when not needed.
    if (no_alternatives):
        return result, []

    # Deduplicate alternatives by (id, set) or id-only depending on mode.
    seen_ids = {best_entry["id"]}
    seen_pairs = {(best_entry["id"], best_entry["set"])}
    alternatives = []
    for score, entry in scores[1:]:
        if (id_only):
            if (entry["id"] in seen_ids):
                continue
            seen_ids.add(entry["id"])
            alt = {"id": int(entry["id"]), "score": score}
        else:
            key = (entry["id"], entry["set"])
            if (key in seen_pairs):
                continue
            seen_pairs.add(key)
            alt = {"id": int(entry["id"]), "set": entry["set"], "score": score}
        alt_conf = score_to_conf(score)
        if (alt_conf < MIN_ALT_CONF):
            continue
        alt["confidence"] = alt_conf
        alternatives.append(alt)
        if (len(alternatives) >= top_k - 1):
            break

    return result, alternatives


# Initialise at import time — works for both `python server.py` and gunicorn.
# Gunicorn configuration disables preloading: OpenCV resources and thread pools
# must be initialized inside each worker, never inherited from the master.
load_index()


@app.before_request
def start_request_timer():
    if (TIMING_LOGS and request.path == "/scan"):
        g.scan_start = (time.perf_counter(), time.process_time())
        g.scan_metrics = {}


@app.after_request
def log_request_timer(response):
    if (hasattr(g, "scan_start")):
        wall, cpu = g.scan_start
        g.scan_metrics["request"] = {
            "wallMs": (time.perf_counter() - wall) * 1000,
            "cpuMs": (time.process_time() - cpu) * 1000,
        }
        app.logger.info("SCAN_TIMING mode=%s status=%s metrics=%s", SEARCH_MODE,
                        response.status_code, json.dumps(g.scan_metrics))
    return response


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/scan", methods=["POST"])
def scan():
    data = request.get_json()
    if (not data or "image" not in data):
        return jsonify({"error": "No image provided"}), 400

    if (not INDEX):
        return jsonify({"error": "Index not loaded. Run build_index.py first."}), 500

    # Decode base64 image
    try:
        img_data = base64.b64decode(data["image"])
        img_array = np.frombuffer(img_data, dtype=np.uint8)
        img = cv2.imdecode(img_array, cv2.IMREAD_GRAYSCALE)
        if (img is None):
            raise ValueError("Could not decode image")
    except Exception as e:
        return jsonify({"error": f"Error decoding image: {str(e)}"}), 400

    id_only = bool(data.get("idOnly", False))
    no_alternatives = bool(data.get("noAlternatives", False))

    t0 = time.perf_counter()
    result, alternatives = match_card(img, id_only=id_only, no_alternatives=no_alternatives,
                                      metrics=getattr(g, "scan_metrics", None))
    elapsed = round((time.perf_counter() - t0) * 1000)

    if (result is None):
        app.logger.info(
            "SCAN not_found | id_only=%s no_alternatives=%s | elapsed=%dms",
            id_only, no_alternatives, elapsed,
        )
        return jsonify({
            "found": False,
            "message": "Not enough features were detected. Improve the lighting or move the card closer.",
            "elapsedMs": elapsed,
        })

    # Confidence level as text
    response = {
        "found": True,
        "id": result["id"],
        "confidence": result["confidence"],
        "score": result["score"],
        "elapsedMs": elapsed,
    }
    if (not id_only):
        response["set"] = result["set"] or "unknown"
    if (not no_alternatives):
        response["alternatives"] = alternatives

    top_set = f" set={result['set']}" if not id_only else ""
    app.logger.info(
        "SCAN found | id=%s%s confidence=%d%% score=%d | id_only=%s no_alternatives=%s | elapsed=%dms",
        result["id"], top_set, result["confidence"], result["score"],
        id_only, no_alternatives, elapsed,
    )
    return jsonify(response)


@app.route("/status")
def status():
    return jsonify({
        "ready": len(INDEX) > 0,
        "cardsIndexed": len(INDEX),
        "indexFile": INDEX_FILE,
    })


if (__name__ == "__main__"):
    print("\n🌐 Server available at: http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
