"""Prove Lamport message ordering and elected-leader recovery on local Docker.

The script creates one hidden event, briefly pauses only TicketFlow followers,
then stops the elected worker while it owns the fixture booking. All scoped
workers are unpaused and restored to three healthy replicas even on failure.
Run only against the local demonstration, without other clients submitting work.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.helpers import ApiClient, assert_inventory, require_local_api, scale_workers, wait_for_idle_queue, worker_containers


def docker(*arguments):
    result = subprocess.run(["docker", *arguments], cwd=ROOT, capture_output=True,
                            text=True, timeout=20, check=False)
    if result.returncode:
        raise RuntimeError(f"Scoped docker {' '.join(arguments)} failed: {result.stderr.strip()}")


def healthy_workers(timeout):
    deadline = time.monotonic() + timeout
    latest = []
    while time.monotonic() < deadline:
        latest = worker_containers()
        if len(latest) == 3 and all(
            item.get("State", {}).get("Running")
            and not item["State"].get("Paused")
            and item["State"].get("Health", {}).get("Status") == "healthy"
            for item in latest
        ):
            return latest
        time.sleep(0.2)
    states = [{"id": item["Id"][:12], "state": item.get("State", {})} for item in latest]
    raise AssertionError(f"Three running, unpaused, healthy TicketFlow workers are required: {states}")


def active_leader(client, deadline, original=None):
    latest = None
    while time.monotonic() < deadline:
        latest = client.request("GET", "/api/coordination")[1]
        if latest.get("active") and latest.get("worker_id") and latest.get("owner_id"):
            if original is None or (
                latest["worker_id"] != original["worker_id"]
                and latest["owner_id"] != original["owner_id"]
                and latest["epoch"] > original["epoch"]
            ):
                return latest
        time.sleep(0.1)
    raise AssertionError(f"An active {'successor' if original else 'leader'} was not observed: {latest}")


def validate_trace(trace):
    expected = ["queued", "claimed", "recovered", "claimed", "confirmed", "observed"]
    events = trace["events"]
    if not trace.get("instrumented") or [item["kind"] for item in events] != expected:
        raise AssertionError(f"Expected a complete instrumented recovery trace: {events}")
    previous = None
    for event in events:
        stamp = event.get("logical_time")
        if isinstance(stamp, bool) or not isinstance(stamp, int) or stamp < 0 or not event.get("process_id"):
            raise AssertionError(f"Invalid logical timestamp or process incarnation: {event}")
        if previous is not None and stamp <= previous:
            raise AssertionError(f"Booking message timestamps are not strictly increasing: {events}")
        if event["kind"] in {"claimed", "recovered", "observed"}:
            if event.get("received_time") != previous or stamp <= event["received_time"]:
                raise AssertionError(f"Lamport receipt did not advance beyond its causal predecessor: {event}")
        previous = stamp
    if events[1]["process_id"] == events[3]["process_id"]:
        raise AssertionError("Both attempts were processed by the same process incarnation.")
    if not events[-1].get("ephemeral"):
        raise AssertionError("The returned API observation must not be a persisted polling event.")
    return events


def validate_recovery_leader(events, successor):
    recovery = events[2]
    if (recovery.get("process_id"), recovery.get("worker_id")) != (
            successor["owner_id"], successor["worker_id"]):
        raise AssertionError("The recorded successor was not the process that recovered this booking.")
    if [event.get("attempt") for event in events] != [0, 1, 1, 2, 2, 2]:
        raise AssertionError("The trace did not preserve the expected two-attempt recovery sequence.")


def restore_workers(client, paused_ids, timeout):
    errors = []
    try:
        # Re-inspect through the label-validated helper; never unpause arbitrary IDs.
        current = {item["Id"]: item for item in worker_containers()}
        for container_id in paused_ids:
            item = current.get(container_id)
            if item and item.get("State", {}).get("Running") and item["State"].get("Paused"):
                try:
                    docker("unpause", container_id)
                except Exception as error:
                    errors.append(str(error))
    except Exception as error:
        errors.append(f"Could not inspect paused workers: {error}")
    try:
        scale_workers(3)
        restored = healthy_workers(timeout)
        wait_for_idle_queue(client, timeout)
    except Exception as error:
        errors.append(f"Could not restore three healthy idle workers: {error}")
    if errors:
        raise RuntimeError("; ".join(errors))
    return restored


def verify(client, timeout=90):
    require_local_api(client)
    client.request("GET", "/api/health")
    wait_for_idle_queue(client, timeout)
    containers = healthy_workers(timeout)
    original = active_leader(client, time.monotonic() + timeout)
    leaders = [item for item in containers if item["Config"].get("Hostname") == original["worker_id"]]
    if len(leaders) != 1:
        raise AssertionError("The elected leader does not identify exactly one scoped worker container.")
    leader_container = leaders[0]
    followers = [item for item in containers if item["Id"] != leader_container["Id"]]
    config = client.request("GET", "/api/dashboard?include_demo=true")[1]["config"]
    if config["processing_seconds"] < 1:
        raise AssertionError("Set PROCESSING_SECONDS to at least 1 for a reliably observable worker interruption.")
    event = client.create_event(3, "syllabus-failover")
    paused_ids = {item["Id"] for item in followers}
    modified = False
    original_error = None
    report = None
    try:
        modified = True
        docker("pause", *sorted(paused_ids))
        # Pausing followers does not revoke an active leader. Detect an unexpected
        # leadership change before submitting, rather than claiming a false proof.
        current = client.request("GET", "/api/coordination")[1]
        if not current.get("active") or (current["owner_id"], current["epoch"]) != (original["owner_id"], original["epoch"]):
            raise AssertionError("Leadership changed before the controlled booking was submitted; rerun the demo.")
        _, booking = client.book(event["id"], customer="Syllabus recovery guest", expected=(202,))
        booking_id = booking["id"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            booking = client.booking(booking_id)
            if booking["status"] == "processing":
                if booking.get("worker_id") != original["worker_id"]:
                    raise AssertionError("The fixture booking was not claimed by the elected leader.")
                break
            if booking["status"] in {"confirmed", "rejected"}:
                raise AssertionError("Booking finished before interruption; no recovery was demonstrated.")
            time.sleep(0.02)
        else:
            raise AssertionError("The elected leader did not claim the fixture booking before the deadline.")

        # Freeze the observed owner before the stop round trip, avoiding a race
        # against the short processing delay. SIGKILL prevents graceful lease
        # release; using stop (rather than kill) suppresses automatic restart.
        paused_ids.add(leader_container["Id"])
        docker("pause", leader_container["Id"])
        frozen = client.booking(booking_id)
        if frozen["status"] != "processing" or frozen.get("worker_id") != original["worker_id"]:
            raise AssertionError("The booking completed before its owner could be frozen; rerun the demo.")
        failure_started = time.monotonic()
        docker("stop", "--signal", "SIGKILL", "--timeout", "0", leader_container["Id"])
        stopped_at = time.monotonic()
        paused_ids.discard(leader_container["Id"])
        docker("unpause", *(item["Id"] for item in followers))
        paused_ids.difference_update(item["Id"] for item in followers)
        after_stop = worker_containers()
        stopped = [item for item in after_stop if item["Id"] == leader_container["Id"]]
        if len(stopped) != 1 or stopped[0].get("State", {}).get("Running"):
            raise AssertionError("The original leader did not remain stopped during recovery.")
        remaining = sum(bool(item.get("State", {}).get("Running")) for item in after_stop)
        if remaining != 2:
            raise AssertionError(f"Expected two running successor candidates, found {remaining}.")

        # One bounded recovery window begins after the deliberate stop completes.
        deadline = stopped_at + timeout
        successor = active_leader(client, deadline, original)
        if any(item["kind"] == "released" and item["owner_id"] == original["owner_id"]
               and item["epoch"] == original["epoch"] for item in successor["history"]):
            raise AssertionError("The interrupted leader released gracefully; abrupt-failure election was not proved.")
        elected_at = time.monotonic()
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            raise AssertionError("The recovery deadline elapsed before the booking could be checked.")
        final = client.wait_for_bookings([booking_id], timeout=remaining_time)[booking_id]
        if final["status"] != "confirmed" or final["attempts"] != 2 or final["worker_id"] == original["worker_id"]:
            raise AssertionError(f"Expected one replacement attempt and one confirmation: {final}")
        inventory = client.event(event["id"])
        assert_inventory(inventory, [final])
        if inventory["confirmed_tickets"] != 1 or inventory["available_seats"] != 2:
            raise AssertionError(f"Recovery debited inventory incorrectly: {inventory}")
        trace = client.request("GET", f"/api/bookings/{booking_id}/trace")[1]
        events = validate_trace(trace)
        validate_recovery_leader(events, successor)
        report = {
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "event_id": event["id"], "booking_id": booking_id,
            "original_leader": original, "successor_leader": successor,
            "original_epoch": original["epoch"], "successor_epoch": successor["epoch"],
            "stopped_container": leader_container["Id"][:12],
            "failure_command": "docker pause leader; docker stop --signal SIGKILL --timeout 0 leader",
            "remaining_workers_during_recovery": remaining,
            "stop_seconds": round(stopped_at - failure_started, 3),
            "election_seconds_after_stop": round(elected_at - stopped_at, 3),
            "recovery_seconds_after_stop": round(time.monotonic() - stopped_at, 3),
            "booking": final, "booking_trace": events, "inventory": inventory,
            "lamport_strictly_increasing": True, "causal_receives_verified": True,
        }
    except BaseException as error:
        original_error = error
        raise
    finally:
        if modified:
            try:
                restored = restore_workers(client, paused_ids, timeout)
                if report is not None:
                    report["workers_restored"] = len(restored)
                    report["restored_worker_ids"] = [item["Config"]["Hostname"] for item in restored]
                    report["restored_workers_healthy"] = True
            except Exception as cleanup_error:
                if original_error is None:
                    raise
                print(f"CLEANUP FAILURE (original verification error preserved): {cleanup_error}", file=sys.stderr)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--timeout", type=float, default=90, help="Seconds for each prerequisite and the post-stop recovery window.")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "syllabus.json")
    arguments = parser.parse_args()
    if arguments.timeout <= 0:
        parser.error("--timeout must be positive")
    report = verify(ApiClient(arguments.base_url), arguments.timeout)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"PASS: higher-epoch leader takeover, two-attempt recovery, one seat debit and Lamport receipts verified.")
    print(f"Evidence: {arguments.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
