"""Original two-worker concurrency; each worker owns its OpenCV resources."""
import os

bind = "0.0.0.0:5000"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
if (workers < 1):
    raise ValueError("WEB_CONCURRENCY must be positive")
worker_class = "sync"
threads = 1
timeout = 60
preload_app = False
