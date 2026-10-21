"""Scalable queue consumer. Kill a container to demonstrate expiring-lease recovery."""

import argparse
import logging
import signal
import socket
import threading
import time

import psycopg

from . import db, leader
from .clock import PROCESS_ID

log = logging.getLogger("ticketflow.worker")


def healthy(worker_id):
    with db.connect() as conn:
        return bool(conn.execute("""SELECT 1 FROM workers WHERE id=%s
            AND heartbeat_at > clock_timestamp()-interval '10 seconds'""", (worker_id,)).fetchone())


def run():
    worker_id = socket.gethostname()
    stopping = threading.Event()
    current_lock = threading.Lock()
    current = None
    leader_epoch = None

    def stop(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    db.register_worker(worker_id)

    def coordinate():
        nonlocal leader_epoch
        state = leader.step(worker_id)
        previous_epoch = leader_epoch
        leader_epoch = state["epoch"] if state["is_leader"] else None
        if leader_epoch is not None and leader_epoch != previous_epoch:
            log.info("Elected recovery leader worker=%s process=%s epoch=%s", worker_id, PROCESS_ID, leader_epoch)
        if state["recovered_count"]:
            log.info("Leader epoch=%s requeued %s expired booking(s)", leader_epoch, state["recovered_count"])

    try:
        coordinate()
    except psycopg.Error:
        log.exception("Initial leader election unavailable; booking processing will retry")

    def renew():
        nonlocal leader_epoch
        while not stopping.wait(db.HEARTBEAT_SECONDS):
            with current_lock:
                claim = current
            try:
                db.heartbeat(worker_id, claim)
                if not stopping.is_set():
                    coordinate()
            except psycopg.Error:
                leader_epoch = None
                log.exception("Heartbeat/election unavailable; expired claims and leader epochs will be fenced")

    pulse = threading.Thread(target=renew, name="lease-heartbeat", daemon=True)
    pulse.start()
    log.info("Worker %s ready, processing=%ss lease=%ss", worker_id, db.PROCESSING_SECONDS, db.LEASE_SECONDS)
    try:
        while not stopping.is_set():
            try:
                claim = db.claim_booking(worker_id)
                if not claim:
                    stopping.wait(0.25)
                    continue
                with current_lock:
                    current = claim
                log.info("Claimed booking=%s attempt=%s", claim["id"], claim["attempts"])
                # A visible delay models external validation; inventory is untouched until commit.
                if stopping.wait(db.PROCESSING_SECONDS):
                    break
                outcome = db.complete_booking(worker_id, claim)
                log.info("Booking=%s result=%s", claim["id"], outcome or "claim expired; skipped")
            except psycopg.Error:
                log.exception("Database unavailable; retrying without acknowledging a booking")
                stopping.wait(1)
            finally:
                with current_lock:
                    current = None
    finally:
        stopping.set()
        pulse.join(timeout=3)
        try:
            # An in-flight database operation must not reacquire after a release.
            # If it has not returned, process exit leaves the lease to expire.
            if not pulse.is_alive():
                leader.release(worker_id, epoch=leader_epoch)
            with db.connect() as conn:
                conn.execute("""UPDATE workers SET heartbeat_at=clock_timestamp()-interval '1 hour',
                    current_booking_id=NULL WHERE id=%s""", (worker_id,))
        except psycopg.Error:
            pass
        log.info("Worker stopped; the elected leader will recover unfinished claims after lease expiry")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--healthcheck", action="store_true")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if arguments.healthcheck:
        try:
            raise SystemExit(0 if healthy(socket.gethostname()) else 1)
        except psycopg.Error:
            raise SystemExit(1)
    run()


if __name__ == "__main__":
    main()
