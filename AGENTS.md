# VTES Card Scanner

Web application that uses the camera to identify **Vampire: The Eternal Struggle (VTES)** cards by matching them against a pre-built ORB feature index. Returns the card ID and, when possible, the exact set/edition.

## Architecture

| File | Role |
|------|------|
| `build_index.py` | Offline step — scans image folders, extracts ORB descriptors, saves `card_index.pkl` |
| `server.py` | Flask server — loads the index, exposes `/scan` (POST) for image matching |
| `index.html` | Single-page frontend — accesses the webcam, sends frames as base64 to `/scan` |
| `card_index.pkl` | Binary index (generated, not in VCS) |

## Image Dataset

Root: configured via `--cards` flag (default `D:\Trabajo\Git\vtesdecks-statics\public\img\cards`)

```
img/cards/
  <id>.jpg              # Primary images — one per card, any edition
  sets/
    <set_abbr>/
      <id>.jpg          # Per-set images — highest priority for matching
  es/  fr/  pt/         # Translated card art — IGNORED for matching
```

- **Only `sets/` and the root** are indexed. Language folders (`es/`, `fr/`, `pt/`) are skipped.
- `sets/` images are indexed first; root images are added as fallback for cards not present in any set folder.
- The dataset is sparse: **one image per card per edition**. This is a known accuracy constraint.

## Matching Pipeline

1. Frontend crops the camera frame to the **centered 5:7 (card-shaped) region** before sending — eliminates desk/background noise.
2. Server resizes the crop to 300×420 px.
3. Apply **CLAHE** (clipLimit=2.0) to normalize contrast — same preprocessing as the index.
4. Extract up to 300 **ORB** keypoints + descriptors.
5. **Phase 1 — BFMatcher** with Hamming distance + **Lowe ratio test** (threshold 0.72) over the full index. Gather all entries with ≥4 good matches.
6. **Phase 2 — Homography/RANSAC** on the top-40 candidates: compute `findHomography` with RANSAC (reproj. error 5 px) and count inliers. Inliers are geometrically consistent matches; they are the actual score.
7. Return best match (ID + set) ranked by inlier count. Confidence = `min(100, inliers × 5)` (20 inliers → 100%).
  - When `idOnly=true`: all editions of a card are merged (max score kept) before ranking — `set` is omitted from results.
  - When `noAlternatives=true`: Phase 2 runs on **top-5** candidates only (vs. top-40), reducing RANSAC calls significantly. Only the best match is returned.

Confidence thresholds: `≥60` → high, `≥35` → medium, `<35` → low.

Runtime defaults: LSH search, a matching pool sized to all detected CPUs,
OpenCV's default parallelism, and one initial synchronous uWSGI worker.
Docker uses `serve.py` only to validate the worker range and exec uWSGI, whose built-in
`cheaper` busyness algorithm scales between `WEB_CONCURRENCY` (default 1)
and `MAX_WEB_CONCURRENCY` (default 3, maximum 3). `uwsgi.ini` must retain `lazy-apps`
and `enable-threads`: initialize OpenCV/index/pool resources after fork and allow
the matching pool to run. Equal worker limits disable adaptive spawning.
`UWSGI_CHEAPER_RSS_LIMIT_SOFT` optionally limits spawning by aggregate worker RSS;
it does not reserve container headroom. Do not reintroduce a custom scaling controller.
Do not introduce internal CPU/thread caps; the production Docker
container controls its CPU allocation. LSH always builds a binary-descriptor
shortlist, expands card IDs to all editions, and uses the same exact scoring.
There is no search-mode switch in configuration, the frontend, or requests. Failed/empty retrieval or no verified result falls
back to exhaustive search; a confidently wrong shortlist does not. See
`PERFORMANCE.md` for environment variables and `benchmark.py` for the standalone
accuracy/performance comparison. Do not introduce a unit-test framework.

> **Index requirement:** the index must be built with the current `build_index.py` (stores CLAHE-preprocessed descriptors **and** keypoint coordinates). A legacy index built without keypoints falls back to raw match count without homography.

## Build & Run

```bash
# 1. Install dependencies
pip install -r requirements.txt   # flask, opencv-python, numpy

# 2. Build the index (run once, or when the image dataset changes)
python build_index.py --cards "path/to/img/cards"

# 3. Start the server
python server.py
# → http://localhost:5000
```

## API

**POST `/scan`**

Request body (JSON):
```json
{
  "image": "<base64-encoded JPEG/PNG>",
  "idOnly": false,
  "noAlternatives": false
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `image` | string | — | Base64-encoded JPEG or PNG |
| `idOnly` | bool | `false` | When `true`, set/edition is ignored — all editions of the same card are merged before ranking. `set` is omitted from the response. |
| `noAlternatives` | bool | `false` | When `true`, only the best match is returned. Phase 2 runs on top-5 candidates instead of top-40, giving a significant speed-up. `alternatives` is omitted from the response. |

Success response (default):
```json
{
  "found": true,
  "id": "100038",
  "set": "30th",
  "confidence": 72,
  "score": 45,
  "elapsedMs": 310,
  "alternatives": [{ "id": "100040", "set": "30th", "score": 30, "confidence": 60 }]
}
```

Success response with `idOnly=true`:
```json
{
  "found": true,
  "id": "100038",
  "confidence": 72,
  "score": 45,
  "elapsedMs": 310,
  "alternatives": [{ "id": "100040", "score": 30, "confidence": 60 }]
}
```

Success response with `noAlternatives=true`:
```json
{
  "found": true,
  "id": "100038",
  "set": "30th",
  "confidence": 72,
  "score": 45,
  "elapsedMs": 310
}
```

Not-found response:
```json
{ "found": false, "message": "...", "elapsedMs": 120 }
```

## Known Limitations & Improvement Directions

- **Single reference image per edition** — heavily tilted cards or strong reflections can still produce errors. The card-crop guide helps; ask the user to keep the card flat and well-lit.
- **Homography needs ≥4 inliers** — very small or heavily occluded cards may not pass. The fallback "not found" is preferable to a wrong answer.
- **Index rebuild required** after preprocessing/descriptor changes in `build_index.py`. With 300 features, descriptors plus keypoints use about 12 KB per image before Python/OpenCV overhead. Runtime search/thread configuration does not require rebuilding. LSH adds a separate in-memory index per worker; measure its memory before deployment.
- **Language folders** — `es/`, `fr/`, `pt/` are intentionally excluded from matching to avoid duplicate noise and because coverage is incomplete.
