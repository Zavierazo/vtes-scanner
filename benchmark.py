"""Standalone accuracy/performance experiment; no unit-test framework required.

Use --help and PERFORMANCE.md. Run only trusted local baseline Python files.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pickle
import platform
import random
import subprocess
import sys
import time
import urllib.request

import cv2
import numpy as np

SEED = 20260912
OPTIONS = [(False, False), (False, True), (True, False), (True, True)]


def memory_mb():
    if (os.name == "nt"):
        class Counters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
                (name, ctypes.c_size_t) for name in (
                    "peak", "rss", "pool_peak", "pool", "nonpaged_peak",
                    "nonpaged", "pagefile", "pagefile_peak")]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32")
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        if (not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb)):
            raise ctypes.WinError()
        return {"rssMb": counters.rss / 1048576, "peakRssMb": counters.peak / 1048576}
    import resource
    rss = None
    if (Path("/proc/self/statm").exists()):
        rss = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1048576
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak /= 1048576 if (sys.platform == "darwin") else 1024
    peak = max(peak, rss or 0)  # Linux RSS counters can be sampled at different instants.
    return {"rssMb": rss, "peakRssMb": peak}


def make_corpus(args):
    with open(args.index, "rb") as stream:
        entries = pickle.load(stream)
    rng = random.Random(SEED)
    groups = {}
    for entry in sorted(entries, key=lambda e: (e["set"] or "", e["id"], e["path"])):
        if (not str(entry["id"]).isdigit()):
            continue  # API IDs are integers; skip artwork filenames when sampling.
        groups.setdefault(entry["set"] or "", []).append(entry)
    for group in groups.values():
        rng.shuffle(group)
    sets = sorted(groups)
    rng.shuffle(sets)
    selected = []
    while (len(selected) < args.samples and sets):
        for name in list(sets):
            selected.append(groups[name].pop())
            if (not groups[name]):
                sets.remove(name)
            if (len(selected) == args.samples):
                break
    corpus = Path(args.output) / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    cases = []
    for number, entry in enumerate(selected):
        if (args.cards):
            root = Path(args.cards)
            folder = root / "sets" / entry["set"] if (entry["set"]) else root
            paths = [folder / f"{entry['id']}{ext}" for ext in (".jpg", ".jpeg", ".png", ".webp")]
            path = next((p for p in paths if p.exists()), paths[0])
        else:
            path = Path(entry["path"])
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if (img is None):
            raise ValueError(f"Cannot read {path}; provide --cards with the dataset root")
        img = cv2.resize(img, (300, 420))
        transform = cv2.getPerspectiveTransform(
            np.float32([[0, 0], [299, 0], [299, 419], [0, 419]]),
            np.float32([[22, 14], [284, 31], [298, 390], [9, 417]]))
        variants = {
            "reference": img,
            "perspective": cv2.warpPerspective(img, transform, (300, 420), borderValue=100),
            "brightness": np.clip(img.astype(np.float32) * 0.55 + 12, 0, 255).astype(np.uint8),
            "blur": cv2.GaussianBlur(img, (7, 7), 1.5),
        }
        for variant in args.variants.split(","):
            image = variants[variant]
            name = f"{number:03d}-{entry['id']}-{entry['set'] or 'root'}-{variant}"
            target = corpus / f"{name}.png"
            if (not cv2.imwrite(str(target), image)):
                raise OSError(f"Cannot write {target}")
            cases.append({"name": name, "file": str(target.resolve()),
                          "id": int(entry["id"]), "set": entry["set"] or "unknown",
                          "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    noise = np.random.default_rng(SEED).integers(0, 256, (420, 300), dtype=np.uint8)
    for name, image in [("blank", np.full((420, 300), 128, np.uint8)), ("non-card-noise", noise)]:
        target = corpus / f"{name}.png"
        if (not cv2.imwrite(str(target), image)):
            raise OSError(f"Cannot write {target}")
        cases.append({"name": name, "file": str(target.resolve()), "id": None, "set": None,
                      "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    return cases


def response_from_match(result, alternatives, id_only, no_alternatives):
    if (result is None):
        return {"found": False}
    response = {"found": True, "id": result["id"], "score": result["score"],
                "confidence": result["confidence"]}
    if (not id_only):
        response["set"] = result["set"] or "unknown"
    if (not no_alternatives):
        response["alternatives"] = alternatives
    return response


def summarize(rows):
    wall = [row["wallMs"] for row in rows if row.get("measured", True)]
    cpu = [row["cpuMs"] for row in rows if row.get("cpuMs") is not None and row.get("measured", True)]
    positives = [r for r in rows if r["expectedId"] is not None]
    negatives = [r for r in rows if r["expectedId"] is None]
    editions = [r for r in positives if not r["idOnly"]]
    return {
        "requests": len(rows), "timedRequests": len(wall), "medianMs": float(np.median(wall)),
        "p95Ms": float(np.percentile(wall, 95)),
        "meanCpuMs": float(np.mean(cpu)) if (cpu) else None,
        "correctCard": sum(r["response"].get("id") == r["expectedId"] for r in positives),
        "cardQueries": len(positives),
        "correctEdition": sum(r["response"].get("id") == r["expectedId"] and
                              r["response"].get("set") == r["expectedSet"] for r in editions),
        "editionQueries": len(editions),
        "falsePositives": sum(r["response"]["found"] for r in negatives),
        "negativeQueries": len(negatives),
        "fallbacks": sum(bool(r.get("metrics", {}).get("fallback")) for r in rows),
    }


class ReusedSearch:
    """Benchmark-only reuse across API flags for the SAME query image.

    Rejected ratio-test pairs cannot affect scoring. Keep only accepted pairs to
    avoid retaining millions of DMatch objects. Cached calls are never timed in
    the summary. The server itself does not use this optimization.
    """

    def __init__(self, matcher):
        self.matcher = matcher
        self.cache = {}

    def knnMatch(self, query, train, k):
        key = id(train)
        if (key not in self.cache):
            pairs = self.matcher.knnMatch(query, train, k=k)
            self.cache[key] = [pair for pair in pairs
                               if (len(pair) == 2 and pair[0].distance < 0.72 * pair[1].distance)]
            return pairs
        return self.cache[key]


def worker(args):
    cases = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    mode = args.worker
    os.environ["INDEX_FILE"] = str(Path(args.index).resolve())
    os.environ["TIMING_LOGS"] = "0"
    os.environ["SEARCH_MODE"] = "lsh" if (mode.startswith("lsh")) else "exact"
    if (mode.startswith("lsh")):
        os.environ["LSH_CANDIDATES"] = mode[3:]
    source = args.baseline if (mode == "baseline") else "server.py"
    if (not source):
        raise ValueError("--baseline is required for baseline mode")
    # Baseline uses its own original thread settings and import initialization.
    cv2.setRNGSeed(SEED)
    started, started_cpu = time.perf_counter(), time.process_time()
    spec = importlib.util.spec_from_file_location("scanner_benchmark_target", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    if (mode == "baseline"):
        # Only redirect the baseline's index filename; retain its algorithm and settings.
        source_text = Path(source).read_text(encoding="utf-8").replace(
            'INDEX_FILE = "card_index.pkl"', f'INDEX_FILE = {str(Path(args.index).resolve())!r}')
        exec(compile(source_text, source, "exec"), module.__dict__)
    else:
        spec.loader.exec_module(module)
    if (not module.INDEX):
        raise ValueError("Index did not load")
    startup = {"wallMs": (time.perf_counter() - started) * 1000,
               "cpuMs": (time.process_time() - started_cpu) * 1000, **memory_mb()}
    images = {c["name"]: cv2.imread(c["file"], cv2.IMREAD_GRAYSCALE) for c in cases}
    cv2.setRNGSeed(SEED)
    module.match_card(images[cases[0]["name"]])  # Unmeasured warmup.
    rows = []
    for repeat in range(args.repeats):
        for case in cases:
            matcher = module.BF_MATCHER
            if (args.reuse_search):
                module.BF_MATCHER = ReusedSearch(matcher)
            for option_number, (id_only, no_alternatives) in enumerate(OPTIONS):
                cv2.setRNGSeed(SEED)
                metrics = {}
                kwargs = {} if (mode == "baseline") else {"metrics": metrics}
                start, cpu = time.perf_counter(), time.process_time()
                result, alternatives = module.match_card(images[case["name"]], id_only=id_only,
                                                          no_alternatives=no_alternatives, **kwargs)
                rows.append({"case": case["name"], "repeat": repeat,
                             "idOnly": id_only, "noAlternatives": no_alternatives,
                             "expectedId": case["id"], "expectedSet": case["set"],
                             "wallMs": (time.perf_counter() - start) * 1000,
                             "cpuMs": (time.process_time() - cpu) * 1000,
                             "measured": not args.reuse_search or option_number == 0,
                             "metrics": metrics,
                             "response": response_from_match(result, alternatives, id_only, no_alternatives)})
            module.BF_MATCHER = matcher
            print(f"{mode}: {repeat + 1}/{args.repeats} {case['name']}", flush=True)
    report = {"mode": mode, "platform": platform.platform(), "python": platform.python_version(),
              "opencv": cv2.__version__, "startup": startup, "memory": memory_mb(),
              "matchThreads": getattr(module, "MATCH_WORKERS", os.cpu_count()),
              "opencvThreads": cv2.getNumThreads(),
              "retrieverReady": getattr(module, "RETRIEVER", None) is not None,
              "indexEntries": len(module.INDEX), "summary": summarize(rows), "rows": rows}
    Path(args.output, f"{mode}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if (module.PHASE1_POOL is not None):
        module.PHASE1_POOL.shutdown()


def container_stats(name):
    # cgroup v2 metrics include all Gunicorn worker processes and queued requests.
    output = subprocess.check_output(["docker", "exec", name, "cat", "/sys/fs/cgroup/cpu.stat",
                                      "/sys/fs/cgroup/memory.peak"], text=True).splitlines()
    usage = next(int(line.split()[1]) for line in output if line.startswith("usage_usec "))
    return {"cpuMs": usage / 1000, "peakMemoryMb": int(output[-1]) / 1048576}


def http_benchmark(args, cases):
    payloads = [(case, id_only, no_alternatives, json.dumps({
        "image": base64.b64encode(Path(case["file"]).read_bytes()).decode(),
        "idOnly": id_only, "noAlternatives": no_alternatives}).encode())
        for _ in range(args.repeats) for case in cases for id_only, no_alternatives in OPTIONS]

    def send(item):
        case, id_only, no_alternatives, payload = item
        start = time.perf_counter()
        req = urllib.request.Request(args.url.rstrip("/") + "/scan", data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as response:
            result = json.load(response)
        elapsed = (time.perf_counter() - start) * 1000
        result.pop("elapsedMs", None)
        result.pop("message", None)
        return {"case": case["name"], "idOnly": id_only, "noAlternatives": no_alternatives,
                "expectedId": case["id"], "expectedSet": case["set"],
                "wallMs": elapsed, "response": result}

    send(payloads[0])  # Warm up before container CPU accounting.
    before = container_stats(args.container) if (args.container) else None
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        rows = list(pool.map(send, payloads))
    total_wall = time.perf_counter() - start
    after = container_stats(args.container) if (args.container) else None
    summary = summarize(rows)
    summary["requestsPerSecond"] = len(rows) / total_wall
    if (before):
        summary["meanCpuMs"] = (after["cpuMs"] - before["cpuMs"]) / len(rows)
        summary["containerPeakMemoryMb"] = after["peakMemoryMb"]
    report = {"url": args.url, "concurrency": args.concurrency, "summary": summary, "rows": rows}
    Path(args.output, f"http-{args.concurrency}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", default="card_index.pkl")
    parser.add_argument("--cards", help="Override stored image paths with dataset root")
    parser.add_argument("--output", default=".local-benchmark")
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--variants", default="reference,perspective,brightness,blur")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--reuse-search", action="store_true",
                        help="Reuse BF pairs across flags; time only default flags, compare all four")
    parser.add_argument("--baseline", help="Trusted pre-change server.py exported from Git")
    parser.add_argument("--modes", default="baseline,exact,lsh50,lsh100,lsh200")
    parser.add_argument("--manifest", help="Reuse generated corpus manifest")
    parser.add_argument("--worker", choices=["baseline", "exact", "lsh50", "lsh100", "lsh200"], help=argparse.SUPPRESS)
    parser.add_argument("--url", help="Benchmark running HTTP server instead of local matchers")
    parser.add_argument("--concurrency", type=int, choices=[1, 2], default=1)
    parser.add_argument("--container", help="Docker container name for cgroup v2 CPU/memory measurement")
    args = parser.parse_args()
    if (args.samples < 1 or args.repeats < 1):
        parser.error("samples and repeats must be positive")
    if (args.worker):
        worker(args)
        return
    Path(args.output).mkdir(parents=True, exist_ok=True)
    cases = json.loads(Path(args.manifest).read_text(encoding="utf-8")) if (args.manifest) else make_corpus(args)
    manifest = Path(args.output, "manifest.json")
    manifest.write_text(json.dumps(cases, indent=2), encoding="utf-8")
    if (args.url):
        http_benchmark(args, cases)
        return
    modes = args.modes.split(",")
    reports = {}
    for mode in modes:
        command = [sys.executable, "-B", __file__, "--worker", mode, "--manifest", str(manifest),
                   "--output", args.output, "--index", args.index, "--repeats", str(args.repeats)]
        if (args.baseline):
            command.extend(["--baseline", args.baseline])
        if (args.reuse_search):
            command.append("--reuse-search")
        subprocess.run(command, check=True)
        reports[mode] = json.loads(Path(args.output, f"{mode}.json").read_text(encoding="utf-8"))
    baseline = reports.get("baseline")
    summary = {}
    for mode, report in reports.items():
        summary[mode] = {**report["summary"], "startup": report["startup"], "memory": report["memory"],
                         "retrieverReady": report["retrieverReady"]}
        if (baseline):
            mismatches = []
            winner_changes = 0
            for old, new in zip(baseline["rows"], report["rows"]):
                if (old["response"] != new["response"]):
                    mismatches.append({"case": new["case"], "idOnly": new["idOnly"],
                                       "noAlternatives": new["noAlternatives"],
                                       "baseline": old["response"], "actual": new["response"]})
                winner_changes += any(old["response"].get(k) != new["response"].get(k)
                                      for k in ("found", "id", "set"))
            summary[mode]["outputMismatches"] = len(mismatches)
            summary[mode]["winnerChanges"] = winner_changes
            Path(args.output, f"{mode}-differences.json").write_text(json.dumps(mismatches, indent=2), encoding="utf-8")
    Path(args.output, "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    if (baseline and "exact" in summary and summary["exact"]["outputMismatches"]):
        raise SystemExit("Exact matching changed baseline outputs; inspect exact-differences.json")


if (__name__ == "__main__"):
    sys.stdout.reconfigure(encoding="utf-8")
    main()
