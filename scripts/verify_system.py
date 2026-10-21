"""Verify a running TicketFlow API; optionally stop a worker and recover its lease.

    python scripts/verify_system.py
    python scripts/verify_system.py --recovery

Recovery immediately stops only a Docker container labelled as project=ticketflow,
service=worker, whose hostname owns this script's isolated demo booking. It stays
stopped while another worker recovers the lease. Three workers are restored in
finally; volumes and showcase events are preserved.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.helpers import ApiClient, assert_inventory, require_local_api, scale_workers, wait_for_idle_queue, worker_containers


def verify_recovery(client, timeout=90):
    require_local_api(client)
    wait_for_idle_queue(client, timeout)
    try:
        scale_workers(3)
        event = client.create_event(3, "lease-recovery")
        _, booking = client.book(event["id"], customer="Lease recovery guest", expected=(202,))
        booking_id = booking["id"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            booking = client.booking(booking_id)
            if booking["status"] == "processing" and booking.get("worker_id"):
                break
            if booking["status"] in {"confirmed", "rejected"}:
                raise AssertionError("Booking finished before its worker could be interrupted; recovery was not demonstrated.")
            time.sleep(0.03)
        else:
            raise AssertionError(f"Booking did not enter processing within {timeout}s: {booking}")

        original_worker = booking["worker_id"]
        targets = [item for item in worker_containers()
                   if item.get("Config", {}).get("Hostname") == original_worker and item.get("State", {}).get("Running")]
        if len(targets) != 1:
            raise RuntimeError(f"Could not safely identify exactly one TicketFlow worker for hostname {original_worker!r}.")
        target = targets[0]
        still_processing = client.booking(booking_id)
        if still_processing["status"] != "processing" or still_processing.get("worker_id") != original_worker:
            raise AssertionError("Booking completed or changed owners before the controlled kill; recovery was not demonstrated.")

        stopped_at = time.monotonic()
        # A manual stop suppresses automatic restart and gives the other workers
        # a chance to reclaim this booking after its last lease renewal expires.
        result = subprocess.run(["docker", "stop", "--time", "0", target["Id"]], cwd=ROOT,
                                capture_output=True, text=True, timeout=20, check=False)
        if result.returncode:
            raise RuntimeError(f"Could not stop the matched TicketFlow worker: {result.stderr}")
        containers = worker_containers()
        stopped = [item for item in containers if item["Id"] == target["Id"]]
        if len(stopped) != 1 or stopped[0].get("State", {}).get("Running"):
            raise AssertionError("The matched worker did not remain stopped; another-worker recovery was not demonstrated.")
        remaining_workers = sum(item.get("State", {}).get("Running", False) for item in containers)
        if remaining_workers != 2:
            raise AssertionError(f"Expected two remaining recovery workers, found {remaining_workers}.")
        final = client.wait_for_bookings([booking_id], timeout=timeout)[booking_id]
        if final["status"] != "confirmed" or final["attempts"] < 2:
            raise AssertionError(f"A leased booking was not successfully reclaimed after worker death: {final}")
        if final["worker_id"] == original_worker:
            raise AssertionError("The stopped worker completed the booking; another-worker recovery was not demonstrated.")
        inventory = client.event(event["id"])
        assert_inventory(inventory, [final])
        assert inventory["confirmed_tickets"] == 1 and inventory["available_seats"] == 2, inventory
        return {"booking_id": booking_id, "event_id": event["id"], "status": final["status"],
                "attempts": final["attempts"], "stopped_container": target["Id"][:12],
                "failure_command": "docker stop --time 0", "remaining_workers_during_recovery": remaining_workers,
                "original_worker_id": original_worker, "recovery_worker_id": final["worker_id"],
                "recovery_elapsed_seconds": round(time.monotonic() - stopped_at, 3),
                "workers_restored": 3,
                "confirmed_tickets": inventory["confirmed_tickets"], "available_seats": inventory["available_seats"]}
    finally:
        scale_workers(3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--recovery", action="store_true", help="Immediately stop a processing worker and verify that another worker recovers its lease.")
    parser.add_argument("--timeout", type=float, default=90, help="Maximum seconds to await each recovery prerequisite/progress check.")
    arguments = parser.parse_args()
    if arguments.timeout <= 0:
        parser.error("--timeout must be positive")
    os.environ["TICKETFLOW_BASE_URL"] = arguments.base_url
    os.environ["TICKETFLOW_REQUIRE_API"] = "1"
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_integration.py", top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun == 0 or result.skipped:
        return 1
    if arguments.recovery:
        print("\nControlled worker failure and lease recovery:", flush=True)
        report = verify_recovery(ApiClient(arguments.base_url), timeout=arguments.timeout)
        print(json.dumps(report, indent=2), flush=True)
    print("PASS: TicketFlow integration checks completed using isolated demo events.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
