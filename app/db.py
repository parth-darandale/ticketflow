"""Short PostgreSQL transactions; the queue and inventory share one commit boundary."""

import os
import uuid
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://ticketflow:ticketflow-local-demo@localhost:5432/ticketflow")
PROCESSING_SECONDS = float(os.environ.get("PROCESSING_SECONDS", "2"))
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "12"))
HEARTBEAT_SECONDS = 2
if not 0 <= PROCESSING_SECONDS <= 30 or LEASE_SECONDS < 6:
    raise ValueError("PROCESSING_SECONDS must be 0..30; LEASE_SECONDS must be at least 6.")


def connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=5)


def initialize():
    with connect() as conn:
        # Serialize schema/seed setup if multiple API replicas boot together.
        conn.execute("SELECT pg_advisory_xact_lock(710212026)")
        conn.execute(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
        seeds = [
            ("midnight-sessions", "Midnight Sessions", "28 OCT 2026", "The Courtyard · Pune", "LIVE MUSIC",
             "An intimate evening of independent sounds, open skies and a little after-hours magic.", 60, 149900, "coral"),
            ("design-futures", "Design Futures", "30 OCT 2026", "Studio 04 · Pune", "TALKS & IDEAS",
             "Fresh perspectives from the people shaping what comes next. Bring your curiosity.", 40, 79900, "teal"),
            ("courtside-collective", "Courtside Collective", "01 NOV 2026", "Arena West · Pune", "SPORT & COMMUNITY",
             "A courtside seat for a day of close games, good company and local sporting talent.", 80, 49900, "violet"),
        ]
        for slug, title, date, venue, category, description, seats, price, accent in seeds:
            event_id = uuid.uuid5(uuid.NAMESPACE_URL, f"ticketflow/{slug}")
            conn.execute("""INSERT INTO events
                (id,slug,title,date_label,venue,category,description,total_seats,available_seats,price_cents,accent)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(slug) DO NOTHING""",
                         (event_id, slug, title, date, venue, category, description, seats, seats, price, accent))


def activity(conn, booking_id, kind, message, worker_id=None):
    conn.execute("INSERT INTO booking_activity(booking_id,kind,message,worker_id) VALUES (%s,%s,%s,%s)",
                 (booking_id, kind, message, worker_id))


def booking_row(conn, booking_id):
    return conn.execute("""SELECT b.id,b.event_id,e.title AS event_title,b.customer_name,b.quantity,b.status,
        b.worker_id,b.attempts,b.created_at,b.started_at,b.finished_at,b.rejection_reason
        FROM bookings b JOIN events e ON e.id=b.event_id WHERE b.id=%s""", (booking_id,)).fetchone()


def register_worker(worker_id):
    with connect() as conn:
        conn.execute("""INSERT INTO workers(id) VALUES (%s) ON CONFLICT(id) DO UPDATE
            SET heartbeat_at=clock_timestamp(),started_at=clock_timestamp(),current_booking_id=NULL""", (worker_id,))


def heartbeat(worker_id, claim=None):
    with connect() as conn:
        # Match completion's booking -> worker lock order to avoid a renewal deadlock.
        if claim:
            conn.execute("""UPDATE bookings SET lease_until=clock_timestamp()+(%s * interval '1 second')
                WHERE id=%s AND claim_token=%s AND status='processing' AND lease_until > clock_timestamp()""",
                         (LEASE_SECONDS, claim["id"], claim["claim_token"]))
        # Only claim/complete transactions change the assignment pointer. An older
        # heartbeat must not restore an assignment that has already finished.
        conn.execute("UPDATE workers SET heartbeat_at=clock_timestamp() WHERE id=%s", (worker_id,))


def claim_booking(worker_id):
    token = uuid.uuid4()
    with connect() as conn:
        claimed = conn.execute("""WITH candidate AS (
            SELECT id,status FROM bookings
            WHERE status='pending' OR (status='processing' AND lease_until < clock_timestamp())
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
        ) UPDATE bookings b SET status='processing',worker_id=%s,claim_token=%s,
            lease_until=clock_timestamp()+(%s * interval '1 second'),attempts=b.attempts+1,
            started_at=COALESCE(b.started_at,clock_timestamp())
        FROM candidate c WHERE b.id=c.id RETURNING b.*,c.status AS previous_status""",
                               (worker_id, token, LEASE_SECONDS)).fetchone()
        if claimed:
            reclaimed = claimed.pop("previous_status") == "processing"
            activity(conn, claimed["id"], "reclaimed" if reclaimed else "claimed",
                     "Expired lease recovered by another worker" if reclaimed else "Worker started checking availability",
                     worker_id)
            conn.execute("UPDATE workers SET current_booking_id=%s,heartbeat_at=clock_timestamp() WHERE id=%s",
                         (claimed["id"], worker_id))
        return claimed


def complete_booking(worker_id, claim):
    """Fence stale workers, reserve seats and finalize the booking in one transaction."""
    with connect() as conn:
        current = conn.execute("""SELECT * FROM bookings WHERE id=%s AND claim_token=%s
            AND status='processing' AND lease_until > clock_timestamp() FOR UPDATE""",
                               (claim["id"], claim["claim_token"])).fetchone()
        if not current:
            return None
        inventory = conn.execute("""UPDATE events SET available_seats=available_seats-%s
            WHERE id=%s AND available_seats >= %s RETURNING available_seats""",
                                 (current["quantity"], current["event_id"], current["quantity"])).fetchone()
        status = "confirmed" if inventory else "rejected"
        reason = None if inventory else "Not enough seats remain for this booking."
        conn.execute("""UPDATE bookings SET status=%s,rejection_reason=%s,finished_at=clock_timestamp(),
            claim_token=NULL,lease_until=NULL WHERE id=%s""", (status, reason, current["id"]))
        conn.execute("""UPDATE workers SET processed_count=processed_count+1,current_booking_id=NULL,
            heartbeat_at=clock_timestamp() WHERE id=%s""", (worker_id,))
        activity(conn, current["id"], status,
                 f"{current['quantity']} ticket(s) confirmed" if inventory else reason, worker_id)
        return status
