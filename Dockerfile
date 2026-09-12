# ---- Stage 1: Build the ORB feature index ----
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY build_index.py .

# Card images are copied from the build context (populated by CI before docker build)
COPY img/cards /build/img/cards

RUN python build_index.py --cards /build/img/cards

# ---- Stage 2: Runtime image ----
FROM python:3.11-slim AS runtime-base

RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM runtime-base AS runtime

COPY server.py index.html gunicorn.conf.py ./

# Only the pre-built index is needed at runtime — card images are NOT included
COPY --from=builder /build/card_index.pkl .

EXPOSE 5000

CMD ["gunicorn", "--config", "gunicorn.conf.py", "server:app"]
