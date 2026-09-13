# Compile uWSGI in a separate stage; keep build tools out of the runtime image.
FROM python:3.11-slim AS dependencies

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# Shared dependencies for offline indexing, production, and benchmarks.
FROM python:3.11-slim AS runtime-base

RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN --mount=from=dependencies,source=/wheels,target=/wheels \
    pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt

# SIGHUP drains active requests; exit-on-reload makes it exit instead of restart.
STOPSIGNAL SIGHUP

# Card images are copied from the build context (populated by CI).
FROM runtime-base AS builder
WORKDIR /build
COPY build_index.py .
COPY img/cards /build/img/cards
RUN python build_index.py --cards /build/img/cards

FROM runtime-base AS runtime
COPY server.py index.html uwsgi.ini serve.py ./

# Only the pre-built index is needed at runtime, not the source card images.
COPY --from=builder /build/card_index.pkl .

EXPOSE 5000
CMD ["python", "serve.py"]
