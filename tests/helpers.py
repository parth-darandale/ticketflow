"""Standard-library HTTP and scoped Docker helpers for disposable demo workloads."""

import json
import os
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
TERMINAL_STATUSES = {"confirmed", "rejected"}


class ApiClient:
    def __init__(self, base_url=None, request_timeout=10):
        self.base_url = (base_url or os.environ.get("TICKETFLOW_BASE_URL", "http://127.0.0.1:8088")).rstrip("/")
        self.request_timeout = request_timeout

    def request(self, method, path, payload=None, key=None, expected=(200,)):
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if key is not None:
            headers["Idempotency-Key"] = str(key)
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            response = urlopen(request, timeout=self.request_timeout)
        except HTTPError as error:
            response = error
        with response:
            status = response.status
            raw = response.read().decode("utf-8")
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError as error:
            raise AssertionError(f"{method} {path} returned non-JSON HTTP {status}: {raw[:300]}") from error
        if status not in expected:
            raise AssertionError(f"{method} {path}: expected HTTP {expected}, received {status}: {body}")
        return status, body

    def create_event(self, seats, prefix="integration"):
        _, event = self.request("POST", "/api/demo/events", {
            "title": f"{prefix}-{uuid.uuid4().hex[:12]}", "total_seats": seats,
        }, expected=(201,))
        if not event.get("is_demo"):
            raise AssertionError(f"Fixture event is not marked as a demo: {event}")
        return event

    def book(self, event_id, quantity=1, customer="Demo guest", key=None, expected=(200, 202)):
        return self.request("POST", "/api/bookings", {
            "event_id": event_id, "customer_name": customer, "quantity": quantity,
        }, key=key or uuid.uuid4(), expected=expected)

    def booking(self, booking_id):
        return self.request("GET", f"/api/bookings/{booking_id}")[1]

    def event(self, event_id):
        return self.request("GET", f"/api/events/{event_id}")[1]

    def wait_for_bookings(self, booking_ids, timeout=90, interval=0.15):
        deadline = time.monotonic() + timeout
        pending = set(booking_ids)
        completed = {}
        latest = {}
        while pending:
            for booking_id in tuple(pending):
                booking = self.booking(booking_id)
                latest[booking_id] = booking
                if booking["status"] in TERMINAL_STATUSES:
                    completed[booking_id] = booking
                    pending.remove(booking_id)
            if not pending:
                return completed
            if time.monotonic() >= deadline:
                states = {booking_id: {"status": item.get("status"), "attempts": item.get("attempts"),
                                       "worker_id": item.get("worker_id")} for booking_id, item in latest.items()}
                raise AssertionError(f"Bookings did not reach a terminal state within {timeout}s: {states}")
            time.sleep(interval)
        return completed


def submit_batch(client, event_id, count, *, quantity=1, concurrency=12, prefix="Batch guest"):
    def submit(ordinal):
        return client.book(event_id, quantity, f"{prefix} {ordinal}", expected=(202,))[1]

    with ThreadPoolExecutor(max_workers=min(concurrency, count)) as pool:
        return list(pool.map(submit, range(count)))


def assert_inventory(event, bookings):
    """Reconcile actual seats against terminal bookings belonging to our event."""
    bookings = list(bookings)
    if any(booking["event_id"] != event["id"] for booking in bookings):
        raise AssertionError("Booking fixtures belong to a different event.")
    if any(booking["status"] not in TERMINAL_STATUSES for booking in bookings):
        raise AssertionError("Inventory should be checked after all fixture bookings finish.")
    confirmed = sum(booking["quantity"] for booking in bookings if booking["status"] == "confirmed")
    assert 0 <= event["available_seats"] <= event["total_seats"], event
    assert event["confirmed_tickets"] == confirmed, (event, bookings)
    assert event["available_seats"] + confirmed == event["total_seats"], (event, bookings)
    counts = event["status_counts"]
    for status in TERMINAL_STATUSES:
        assert counts.get(status, 0) == sum(booking["status"] == status for booking in bookings), (counts, bookings)
    assert sum(counts.values()) == len(bookings), (counts, bookings)


def compose(*arguments, timeout=90):
    command = ["docker", "compose", "-p", "ticketflow", *arguments]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"{' '.join(command)} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def worker_containers():
    identifiers = compose("ps", "--all", "--quiet", "worker").split()
    if not identifiers:
        return []
    result = subprocess.run(["docker", "inspect", *identifiers], cwd=ROOT, capture_output=True,
                            text=True, timeout=20, check=False)
    if result.returncode:
        raise RuntimeError(f"Could not inspect TicketFlow workers: {result.stderr}")
    containers = json.loads(result.stdout)
    for container in containers:
        labels = container.get("Config", {}).get("Labels", {})
        if labels.get("com.docker.compose.project") != "ticketflow" or labels.get("com.docker.compose.service") != "worker":
            raise RuntimeError("Refusing to operate on a container outside the TicketFlow worker service.")
    return containers


def scale_workers(count):
    compose("up", "-d", "--no-deps", "--scale", f"worker={count}", "worker")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        running = [item for item in worker_containers() if item.get("State", {}).get("Running")]
        if len(running) == count:
            return running
        time.sleep(0.2)
    raise RuntimeError(f"TicketFlow did not reach {count} running workers within 30 seconds.")


def require_local_api(client):
    if urlparse(client.base_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Worker scaling/recovery requires a loopback TicketFlow API URL.")


def wait_for_idle_queue(client, timeout=90):
    """Drain pre-existing work before scaling or introducing controlled failure."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        metrics = client.request("GET", "/api/dashboard?include_demo=true")[1]["metrics"]
        if metrics["queue_depth"] == 0:
            return
        time.sleep(0.25)
    raise AssertionError(f"Existing queue did not drain within {timeout}s; worker demonstration was not started.")
