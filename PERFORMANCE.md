# Scanner performance experiments

LSH search is always enabled, with exhaustive search as a fallback. No image-index rebuild or API/client change is
needed. The matching pool uses all detected logical CPUs, OpenCV retains its
default parallelism, and uWSGI starts one worker and automatically scales up to
three using its built-in `cheaper` busyness algorithm.
There are no application-level CPU/thread caps: control CPU allocation through
Docker, as in the production deployment (3–4 CPUs).

## Runtime configuration

| Environment variable | Default | Meaning |
|---|---|---|
| `WEB_CONCURRENCY` | `1` | Initial and minimum synchronous uWSGI workers |
| `MAX_WEB_CONCURRENCY` | `3` | Maximum workers (1–3); set equal to `WEB_CONCURRENCY` for fixed concurrency |
| `UWSGI_CHEAPER_RSS_LIMIT_SOFT` | unset | Optional total worker RSS threshold in bytes that blocks further spawning |
| `LSH_CANDIDATES` | `100` | Images shortlisted by LSH before expanding their card IDs to all editions |
| `TIMING_LOGS` | `0` | Set to `1` for JSON stage timings in server logs |
| `INDEX_FILE` | `card_index.pkl` | Optional existing index path |

Docker runs `python serve.py`, a small launcher that validates the concurrency
range and replaces itself with uWSGI using `os.execvp`. There is no custom scaling
controller. uWSGI serves HTTP directly on port 5000 with `uwsgi.ini`, so the
existing reverse proxy can continue forwarding HTTP.

The `busyness` algorithm samples worker utilization over 15 seconds. Above 70%
average utilization it can add one worker; below 20% it waits about three minutes
before removing one. The delay can grow when workers are repeatedly stopped and
respawned. A queue exceeding 33 connections, or staying nonempty for 15 seconds,
can also add one worker. New workers must load the index and build LSH before
serving requests. The ceiling remains three, including these backlog responses.
See [uWSGI cheaper documentation](https://uwsgi.readthedocs.io/en/latest/Cheaper.html).

Keep `lazy-apps = true` so OpenCV objects, LSH indexes, and thread pools are created
inside each worker after fork. Keep `enable-threads = true` so the application's
matching pool runs. Each worker handles one HTTP request at a time. The pool size
still follows `os.cpu_count()` and OpenCV's default parallelism is retained.

Each worker retains its own index. The optional `UWSGI_CHEAPER_RSS_LIMIT_SOFT`
uses the current sum of worker RSS, not container headroom or a reservation for
the next worker. Leave room below the Docker memory limit for another worker's
startup peak, the master, and other container memory. This threshold is disabled
unless configured; Docker enforces the actual memory limit. The previous custom
scaler's `SCALE_*` and `WORKER_MEMORY_RESERVE_MB` settings no longer apply.

Requests have a 60-second uWSGI harakiri timeout. The image uses `STOPSIGNAL SIGHUP`
with `exit-on-reload` for graceful shutdown; SIGTERM performs an immediate stop.
Workers have 65 seconds to finish during scale-down/graceful shutdown; use
`docker stop --time 70` (or an equivalent deployment stop timeout, preserving the
image's stop signal) to let this grace period complete. Application scan/timing logs and
uWSGI scaling logs go to container output; routine HTTP access logging is disabled.

`python server.py` remains the development server, including on Windows. Production
uWSGI is installed only on Linux. `serve.py` accepts an optional WSGI module argument
for diagnostics; it does not accept Gunicorn CLI flags or `GUNICORN_CMD_ARGS`.
Restart the container after changing configuration. No card-index rebuild is
required by this server migration.

There is no search-mode environment setting, web selector, or request-level
mode override; `/scan`
continues to accept the same documented fields as before.

`SCAN_TIMING` logs include preprocessing (resize, CLAHE, ORB), optional retrieval,
exact descriptor search, homography calls, full matching, and request handling.
Each stage reports `wallMs` and process `cpuMs`; fallback searches accumulate in
the same stage counters. Request time includes JSON parsing, decoding, matching,
and response construction, but excludes time queued before Flask, network
transfer, and frontend metadata requests. Process CPU includes all matching
threads. Use synchronous uWSGI workers for interpretable per-request CPU
accounting; concurrent development-server requests can overlap process counters.

## LSH behavior

LSH builds a binary Hamming index once per worker (6 tables, 20-bit keys, one
multi-probe level). Each query descriptor retrieves up to eight neighbors and
casts at most one vote per indexed image. Images rank by votes, with original
index order breaking ties. All editions of each shortlisted card ID are included
before exact matching, in original index order.

The existing per-image Lowe ratio test (0.72), top-40/top-5 verification windows,
homography, confidence, and result formatting are retained. There is no global
ratio test. Index construction/retrieval exceptions, empty retrieval, or no
verified shortlisted result trigger exhaustive search. A shortlisted result
that is confidently wrong does **not** trigger fallback. Alternatives and scores
can also differ if retrieval excludes a baseline candidate.

LSH consumes extra RAM and startup CPU. An operating-system OOM kill cannot be
caught by Python's fallback handler. Real camera accuracy still needs validation, and the VPS needs adequate measured
memory headroom.
Synthetic transformations are useful for comparisons but do not model glare,
camera autofocus, background clutter, or all edition ambiguities.

## Reproduce the local comparison

Run from the repository root with the existing requirements installed. Export the
pre-change baseline from commit `bb09b4c38848a93259ac28a3c9ecc6d7bc84d436`:

```bash
git show bb09b4c38848a93259ac28a3c9ecc6d7bc84d436:server.py > .local-baseline.py
python benchmark.py --baseline .local-baseline.py --cards /path/to/img/cards
```

PowerShell users can export the baseline without changing its encoding:

```powershell
python -c "import pathlib,subprocess; pathlib.Path('.local-baseline.py').write_bytes(subprocess.check_output(['git','show','bb09b4c38848a93259ac28a3c9ecc6d7bc84d436:server.py']))"
python benchmark.py --baseline .local-baseline.py --cards 'D:\Trabajo\Git\vtesdecks-statics\public\img\cards'
```

The script samples 30 reference images with numeric card IDs across sets using seed `20260912`. It
creates reference, perspective, brightness, and blur variants, plus blank and
random-noise negatives. The full search index is retained, including all editions.
Each of the 122 inputs runs with all four API flag combinations in fresh processes
for baseline, exact, LSH-50, LSH-100, and LSH-200. OpenCV's RNG is reset before each
comparison. The exact control disables LSH initialization only inside the benchmark;
production has no mode switch. One initial warmup is excluded from timings.

Results are written to ignored `.local-benchmark/`: the corpus manifest includes
image hashes and expected IDs/editions; per-mode JSON includes raw results, stage
timings, runtime versions, startup/current/peak process memory, and summaries.
Difference files compare the complete result (including scores and alternatives),
and the command fails if exact mode differs from the baseline. Measured accuracy
against the expected card is reported separately from baseline agreement.

Useful switches:

- `--samples 1 --output .local-smoke` for a short run.
- `--repeats 3` for repeated latency measurements.
- `--modes exact,lsh100` to omit baseline or restrict experiments.
- `--manifest /path/to/manifest.json` to reuse a fixed corpus or supply real camera
  captures. Each entry needs `name`, absolute `file`, numeric `id` (or null for a
  negative), and `set` (or `unknown` for an unlabelled edition).
- `--reuse-search` to accelerate the four-flag accuracy comparison. Only the first
  (default-flags) scan per image is included in latency/CPU summaries. Remaining
  flag combinations reuse accepted BF pairs for the same image and still rerun
  ranking/homography. This cache exists only in the benchmark. Use the default
  uncached mode or HTTP measurements for all-flags latency. The first timed call
  includes the small cost of populating this benchmark cache in both modes.

The baseline file must be trusted Python code. Only its hardcoded index path is
redirected by the benchmark; its original algorithm and thread settings remain.
Comparisons must use the same index and OpenCV version. Corpus sampling skips
non-numeric artwork filenames because the existing API exposes integer IDs.

## Docker CPU limits and concurrent HTTP requests

The `runtime-base` target installs the same dependencies as production without
rebuilding the image dataset. The following commands use Bash on the VPS and mount
the existing local index and code for an experiment; they do not deploy anything.

```bash
docker build --target runtime-base -t vtes-scanner:benchmark-base .

# Complete direct comparison inside a four-vCPU container.
docker run --rm --cpus 4 \
  -v "$PWD:/app" -v /path/to/img/cards:/cards:ro -w /app \
  vtes-scanner:benchmark-base python -B benchmark.py \
  --baseline .local-baseline.py --cards /cards --reuse-search \
  --output .local-benchmark-docker

# HTTP workload: repeat for cpus=3 and cpus=4 (LSH with exhaustive fallback).
docker run -d --name vtes-perf --cpus 3 -p 127.0.0.1:5055:5000 \
  -v "$PWD:/app:ro" -w /app \
  -e WEB_CONCURRENCY=1 -e MAX_WEB_CONCURRENCY=1 -e TIMING_LOGS=1 \
  vtes-scanner:benchmark-base python serve.py
curl --fail http://localhost:5055/status

python benchmark.py --url http://localhost:5055 --container vtes-perf \
  --cards /path/to/img/cards --samples 30 --concurrency 1 \
  --output .local-http-3cpu-lsh
python benchmark.py --url http://localhost:5055 --container vtes-perf \
  --manifest .local-http-3cpu-lsh/manifest.json --concurrency 2 \
  --output .local-http-3cpu-lsh
docker logs vtes-perf
docker stop --time 70 vtes-perf
docker rm vtes-perf
```

For an autoscaling experiment, set `MAX_WEB_CONCURRENCY=3`, use sustained concurrent
requests, and include worker startup and scale-down delay in the observation period.
For fixed two- or three-worker comparisons, set both concurrency variables equally.

Wait for `/status` to report ready before measuring. LSH uses 100 preliminary
candidates by default; adjust with `-e LSH_CANDIDATES=100` if needed. Use separate
output directories per CPU/candidate/worker setting. HTTP timings include queueing and
network time. With `--container`, cgroup v2 CPU deltas account for all workers;
peak memory is the container lifetime high-water mark, including startup/warmup
and filesystem cache. Start a fresh container for independent memory comparisons.
Without this option HTTP CPU/memory are unavailable, not zero.

HTTP runs issue two simultaneous warmup requests before measurement, supporting
comparisons with two workers. With the default single worker, these requests are
handled sequentially; direct matcher runs use one warmup scan.

Run concurrent client requests even with one worker: that measures actual queueing
on a constrained VPS. Do not run other benchmark configurations simultaneously.
