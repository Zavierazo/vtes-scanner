# VTES Card Scanner

Web application that uses the camera to identify Vampire: The Eternal Struggle (VTES) cards by matching them against a pre-built ORB feature index.

## Quick Overview

- Frontend: single-page `index.html` — captures a center crop (5:7 card ratio) and sends base64 frames to the server.
- Backend: `server.py` — Flask app that loads the prebuilt `card_index.pkl` and exposes `/scan` POST endpoint.
- Offline index builder: `build_index.py` — extracts ORB descriptors and builds `card_index.pkl`.

## Prerequisites

- Python 3.8+
- Git (optional)

## Setup

```bash
# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

```powershell
# Windows
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## Build the image index

You can use the `vtesdecks-statics` repository as the card image source: https://github.com/Zavierazo/vtesdecks-statics

```bash
python build_index.py --cards "path/to/img/cards"
```

The builder creates `card_index.pkl` (binary). This file is large and intentionally excluded from VCS.

## Run the server

```bash
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
  "noAlternatives": false,
  "fast": false
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `image` | string | — | Base64-encoded JPEG or PNG |
| `idOnly` | bool | `false` | Ignore set/edition — all editions of a card are merged; `set` is omitted from the response |
| `noAlternatives` | bool | `false` | Return only the top match (faster — Phase 2 uses top-5 instead of top-40); `alternatives` is omitted |
| `fast` | bool | `false` | Use `nfeatures=100` instead of 300 for quicker feature extraction; reduces accuracy slightly |

Default success response:
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

Not-found response:
```json
{ "found": false, "message": "...", "elapsedMs": 120 }
```

## Docker

A `Dockerfile` is present for container usage:

```powershell
docker build -t vtes-scanner .
docker run -p 5000:5000 -v C:\path\to\card_index.pkl:/app/card_index.pkl vtes-scanner
```

## Notes & Limitations

- The dataset uses one reference image per card/edition — coverage is sparse; tilt and reflections can affect matching.
- Homography requires ≥4 inliers for geometric verification; heavily occluded cards may return "not found".
- Rebuild the index after updating `build_index.py` or the card dataset.

## Where to look

- Server: [server.py](server.py)
- Index builder: [build_index.py](build_index.py)
- Frontend: [index.html](index.html)

---

If you'd like, I can also: run the server locally, test the `/scan` route with a sample image, or add a Makefile/PowerShell script to automate setup.
