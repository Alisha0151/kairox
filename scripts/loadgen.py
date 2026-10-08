"""Steady synthetic traffic against the demo gateway (stdlib only).

    python scripts/loadgen.py --url http://localhost:9100 --rps 8
"""
from __future__ import annotations

import argparse
import json
import random
import threading
import time
import urllib.request


def one(url: str) -> int:
    body = {"sku": random.choice(["sku-1", "sku-2", "sku-3"]), "qty": 1, "amount": random.randint(5, 200)}
    req = urllib.request.Request(url.rstrip("/") + "/checkout", json.dumps(body).encode(),
                                 {"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 599


def run(url: str, rps: float, stop: threading.Event, duration: float | None = None) -> None:
    end = time.time() + duration if duration else None
    while not stop.is_set() and (end is None or time.time() < end):
        threading.Thread(target=one, args=(url,), daemon=True).start()
        time.sleep(1.0 / rps)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:9100")
    ap.add_argument("--rps", type=float, default=8)
    a = ap.parse_args()
    print(f"load: {a.rps} rps -> {a.url}", flush=True)
    run(a.url, a.rps, threading.Event())
