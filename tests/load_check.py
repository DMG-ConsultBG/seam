# -*- coding: utf-8 -*-
"""Where this thing breaks.

Not part of the suite - it takes about a minute and the numbers depend on the
machine. Run it deliberately:

    .venv\\Scripts\\python tests\\load_check.py

SQLite with WAL allows many readers and one writer at a time. The question
worth answering before a customer answers it for you is: at how many people
does the writer become the queue, and what does a request cost when it does.
"""
import os
import sys
import time
import json
import statistics
import threading
import urllib.request
import urllib.error
import http.cookiejar

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from seamclient import Client, BASE

CONCURRENCY = [1, 4, 8, 16, 32]
PER_WORKER = 12


def _session():
    c = Client()
    st, r = c.login()
    if st != 200:
        raise SystemExit("cannot sign in at %s (start Seam first)" % BASE)
    return c


def _timed(fn):
    t0 = time.perf_counter()
    ok = True
    try:
        st = fn()
        ok = st < 400
    except Exception:
        ok = False
    return (time.perf_counter() - t0) * 1000.0, ok


def measure(label, make_call, workers, per_worker=PER_WORKER):
    """Run `workers` sessions in parallel, each doing `per_worker` calls."""
    lat, fails = [], [0]
    lock = threading.Lock()

    def run():
        c = _session()
        mine = []
        for _ in range(per_worker):
            ms, ok = _timed(lambda: make_call(c))
            mine.append(ms)
            if not ok:
                with lock:
                    fails[0] += 1
        with lock:
            lat.extend(mine)

    threads = [threading.Thread(target=run) for _ in range(workers)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0

    lat.sort()
    return {
        "what": label, "workers": workers, "calls": len(lat),
        "per_second": round(len(lat) / wall, 1) if wall else 0,
        "median_ms": round(statistics.median(lat), 1) if lat else 0,
        "p95_ms": round(lat[int(len(lat) * 0.95) - 1], 1) if lat else 0,
        "worst_ms": round(lat[-1], 1) if lat else 0,
        "failed": fails[0],
    }


def read_call(c):
    st, _r = c.call("GET", "/api/workspaces")
    return st


def heavy_read_call(c):
    st, _r = c.call("GET", "/api/analytics?days=90")
    return st


def write_call(c):
    ws, order = getattr(c, "_target", (None, None))
    if ws is None:
        ws, order = c.first_order()
        c._target = (ws, order)
    st, _r = c.call("POST", "/api/orders/%d/comments" % order["id"],
                    {"body": "load check %d" % time.time_ns()})
    return st


def main():
    print("Seam load check against %s\n" % BASE)
    rows = []
    for label, fn, steps in (("read: the home list", read_call, CONCURRENCY),
                             ("read: analytics over 90 days", heavy_read_call, [1, 4, 8, 16]),
                             ("write: a comment", write_call, CONCURRENCY)):
        for n in steps:
            r = measure(label, fn, n)
            rows.append(r)
            print("  %-30s %2d at once  %6.1f/s  median %6.1f ms  p95 %7.1f ms  "
                  "worst %7.1f ms  failed %d"
                  % (r["what"], r["workers"], r["per_second"], r["median_ms"],
                     r["p95_ms"], r["worst_ms"], r["failed"]))
        print()

    out = os.path.join(HERE, "load_check.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print("written to %s" % out)

    worst = max(rows, key=lambda r: r["p95_ms"])
    print("\nSlowest case: %s at %d concurrent, p95 %.0f ms."
          % (worst["what"], worst["workers"], worst["p95_ms"]))
    broken = [r for r in rows if r["failed"]]
    print("Failures: %s" % ("none" if not broken else
                            ", ".join("%s at %d (%d)" % (r["what"], r["workers"], r["failed"])
                                      for r in broken)))


if __name__ == "__main__":
    main()
