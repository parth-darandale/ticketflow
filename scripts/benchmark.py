"""Measure an isolated booking batch with one worker and then three workers.

    python scripts/benchmark.py --orders 12

Requires a running Docker Compose project named ticketflow. Scaling and startup
are excluded from timing. This is observed local throughput, not a guarantee of
linear scaling. All phases use separate hidden demo events, and three workers
are restored even if a phase fails. No volumes or existing events are deleted.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.helpers import ApiClient, assert_inventory, require_local_api, scale_workers, submit_batch, wait_for_idle_queue


def measure(client, worker_count, orders, timeout):
    wait_for_idle_queue(client, timeout)
    containers = scale_workers(worker_count)
    # Allow fresh worker processes to establish their first database connections.
    time.sleep(2.1)
    event = client.create_event(orders, f"benchmark-{worker_count}-workers")
    started = time.monotonic()
    submitted = submit_batch(client, event["id"], orders, concurrency=min(12, orders), prefix="Benchmark guest")
    submitted_at = time.monotonic()
    final = client.wait_for_bookings([booking["id"] for booking in submitted], timeout=timeout)
    finished = time.monotonic()
    rejected = [booking for booking in final.values() if booking["status"] != "confirmed"]
    if rejected:
        raise AssertionError(f"Benchmark capacity matched demand, but bookings were rejected: {rejected}")
    inventory = client.event(event["id"])
    assert_inventory(inventory, final.values())
    if inventory["available_seats"] != 0 or inventory["confirmed_tickets"] != orders:
        raise AssertionError(f"Benchmark inventory did not reconcile: {inventory}")
    elapsed = finished - started
    observed_workers = sorted({booking["worker_id"] for booking in final.values()})
    if len(observed_workers) != worker_count:
        raise AssertionError(f"Expected {worker_count} workers to handle this phase, observed {observed_workers}")
    return {"configured_workers": worker_count, "running_worker_containers": len(containers),
            "observed_worker_ids": observed_workers, "event_id": event["id"],
            "bookings": orders, "confirmed": len(final), "rejected": 0,
            "submission_seconds": round(submitted_at - started, 3),
            "elapsed_seconds": round(elapsed, 3), "bookings_per_second": round(orders / elapsed, 4),
            "attempts": sum(booking["attempts"] for booking in final.values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--orders", type=int, default=12, help="Bookings in each phase (3 through 100; default 12).")
    parser.add_argument("--timeout", type=float, default=300, help="Maximum booking-completion wait per phase in seconds.")
    parser.add_argument("--output", type=Path, help="JSON report path; defaults to artifacts/benchmark-<UTC timestamp>.json.")
    arguments = parser.parse_args()
    if not 3 <= arguments.orders <= 100:
        parser.error("--orders must be between 3 and 100")
    if arguments.timeout <= 0:
        parser.error("--timeout must be positive")
    client = ApiClient(arguments.base_url)
    require_local_api(client)
    runtime_config = client.request("GET", "/api/dashboard")[1]["config"]
    measurements = []
    try:
        for workers in (1, 3):
            print(f"Measuring {arguments.orders} isolated bookings with {workers} worker(s)...", flush=True)
            result = measure(client, workers, arguments.orders, arguments.timeout)
            measurements.append(result)
            print(json.dumps(result, indent=2), flush=True)
    finally:
        scale_workers(3)
    serial, parallel = measurements
    ratio = serial["elapsed_seconds"] / parallel["elapsed_seconds"]
    now = datetime.now(timezone.utc)
    report = {"recorded_at_utc": now.isoformat(), "base_url": client.base_url,
              "runtime_config": runtime_config,
              "workload": "One ticket per asynchronous booking; separate demo event for each phase.",
              "timing": "HTTP batch submission through all terminal bookings; excludes Docker scaling/startup.",
              "limitations": "Local measurement includes API, database, polling and any other queued work; no linear-speedup guarantee.",
              "measurements": measurements, "observed_speedup_one_to_three": round(ratio, 3),
              "workers_restored": 3}
    destination = arguments.output or ROOT / "artifacts" / f"benchmark-{now.strftime('%Y%m%dT%H%M%S%fZ')}.json"
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Observed speedup (1 worker / 3 workers elapsed): {ratio:.2f}x", flush=True)
    print(f"Report: {destination}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
