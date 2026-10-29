"""HTTP API and browser dashboard. Booking acceptance commits a durable queue row."""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import psycopg
from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import db, leader
from .clock import CLOCK, PROCESS_ID
from .control import router as control_router


@asynccontextmanager
async def lifespan(_app):
    db.initialize()
    yield


app = FastAPI(title="TicketFlow", version="1.2.0", lifespan=lifespan,
              description="Distributed demo reservations. No payments or real tickets are issued.")
app.include_router(control_router)


@app.exception_handler(psycopg.Error)
async def database_error(_request, _error):
    return JSONResponse(status_code=503, content={"detail": "Database temporarily unavailable. Please retry using the same booking request."})


class BookingRequest(BaseModel):
    event_id: uuid.UUID
    customer_name: str = Field(min_length=1, max_length=60)
    quantity: int = Field(ge=1, le=6, strict=True)

    @field_validator("customer_name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Please enter a name or alias.")
        return value


class DemoEventRequest(BaseModel):
    title: str = Field(min_length=3, max_length=80)
    total_seats: int = Field(ge=1, le=500, strict=True)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value):
        value = value.strip()
        if len(value) < 3:
            raise ValueError("Please provide an event title with at least three characters.")
        return value


@app.get("/api/health")
def health():
    with db.connect() as conn:
        conn.execute("SELECT 1 FROM bookings LIMIT 1")
    return {"status": "ok", "database": "connected"}


@app.get("/api/events/{event_id}")
def event_detail(event_id: uuid.UUID):
    with db.connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        event = conn.execute("SELECT * FROM events WHERE id=%s", (event_id,)).fetchone()
        if not event:
            raise HTTPException(404, "Event not found.")
        counts = conn.execute("SELECT status,count(*) AS count FROM bookings WHERE event_id=%s GROUP BY status",
                              (event_id,)).fetchall()
        event["status_counts"] = {status: 0 for status in ("pending", "processing", "confirmed", "rejected")}
        event["status_counts"].update({row["status"]: row["count"] for row in counts})
        event["confirmed_tickets"] = event["total_seats"] - event["available_seats"]
        return event


@app.post("/api/demo/events", status_code=201)
def create_demo_event(body: DemoEventRequest):
    event_id = uuid.uuid4()
    with db.connect() as conn:
        return conn.execute("""INSERT INTO events(id,slug,title,date_label,venue,category,description,
            total_seats,available_seats,is_demo) VALUES (%s,%s,%s,'DEMO EVENT','Local demo','SCALING DEMO',
            'An isolated event for concurrency and scaling demonstrations.',%s,%s,true) RETURNING *""",
                            (event_id, f"demo-{event_id.hex}", body.title.strip(), body.total_seats, body.total_seats)).fetchone()


@app.post("/api/bookings", status_code=202)
def submit_booking(body: BookingRequest, response: Response,
                   idempotency_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")]):
    with db.connect() as conn:
        existing = conn.execute("SELECT * FROM bookings WHERE idempotency_key=%s", (idempotency_key,)).fetchone()
        if existing:
            inserted = False
        else:
            if not conn.execute("SELECT id FROM events WHERE id=%s", (body.event_id,)).fetchone():
                raise HTTPException(404, "Event not found.")
            booking_id = uuid.uuid4()
            inserted = conn.execute("""INSERT INTO bookings(id,event_id,idempotency_key,customer_name,quantity)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT(idempotency_key) DO NOTHING RETURNING *""",
                                    (booking_id, body.event_id, idempotency_key, body.customer_name, body.quantity)).fetchone()
            existing = inserted or conn.execute("SELECT * FROM bookings WHERE idempotency_key=%s",
                                                (idempotency_key,)).fetchone()
        if (existing["event_id"], existing["customer_name"], existing["quantity"]) != (
                body.event_id, body.customer_name, body.quantity):
            raise HTTPException(409, "This request key was already used for a different booking.")
        if inserted:
            stamp = CLOCK.tick()
            conn.execute("UPDATE bookings SET last_logical_time=%s WHERE id=%s", (stamp, existing["id"]))
            db.activity(conn, existing["id"], "queued", "API sent booking to the durable queue", logical_time=stamp)
        else:
            response.status_code = 200
        result = db.booking_row(conn, existing["id"])
        result["replayed"] = not bool(inserted)
        result["trace_url"] = f"/api/bookings/{existing['id']}/trace"
        return result


@app.get("/api/bookings/{booking_id}")
def booking_detail(booking_id: uuid.UUID):
    with db.connect() as conn:
        result = db.booking_row(conn, booking_id)
        if not result:
            raise HTTPException(404, "Booking not found.")
        return result


@app.get("/api/bookings/{booking_id}/trace")
def booking_trace(booking_id: uuid.UUID):
    with db.connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        booking = db.booking_row(conn, booking_id)
        if not booking:
            raise HTTPException(404, "Booking not found.")
        events = conn.execute("""SELECT id,kind,message,worker_id,process_id,logical_time,received_time,
            attempt,created_at FROM booking_activity WHERE booking_id=%s ORDER BY id""", (booking_id,)).fetchall()
    incoming = booking["last_logical_time"]
    if incoming is not None:
        # Receipt of a persisted worker message advances the API's shared clock.
        # This observation is returned only; polling does not append database rows.
        events.append({"id": None, "kind": "observed", "message": "API received this booking state",
                       "worker_id": None, "process_id": PROCESS_ID, "logical_time": CLOCK.receive(incoming),
                       "received_time": incoming, "attempt": booking["attempts"],
                       "created_at": datetime.now(timezone.utc), "ephemeral": True})
    return {"booking_id": booking_id, "instrumented": incoming is not None, "events": events,
            "explanation": "Process-local Lamport ticks order this booking's messages; they are not wall-clock time. "
                           "Counters alone cannot prove causality between unrelated bookings. "
                           "NULL timestamps identify historical events without instrumentation."}


@app.get("/api/coordination")
def coordination():
    with db.connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return coordination_snapshot(conn)


def coordination_snapshot(conn):
    result = leader.snapshot(conn)
    result.update(api_process_id=PROCESS_ID, api_logical_time=CLOCK.value)
    return result


@app.get("/api/dashboard")
def dashboard(include_demo: bool = False):
    with db.connect() as conn:
        # One snapshot prevents cards and totals from representing different commits.
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        events = conn.execute("SELECT * FROM events WHERE NOT is_demo OR %s ORDER BY created_at,id",
                              (include_demo,)).fetchall()
        bookings = conn.execute("""SELECT b.id,b.event_id,e.title AS event_title,b.customer_name,b.quantity,
            b.status,b.worker_id,b.attempts,b.created_at,b.started_at,b.finished_at,b.rejection_reason
            FROM bookings b JOIN events e ON e.id=b.event_id WHERE NOT e.is_demo OR %s
            ORDER BY b.created_at DESC LIMIT 40""", (include_demo,)).fetchall()
        workers = conn.execute("""SELECT w.id,w.started_at,w.heartbeat_at,w.processed_count,w.process_id,w.logical_time,
            b.id AS current_booking_id,w.heartbeat_at > clock_timestamp()-interval '10 seconds' AS online,
            CASE WHEN w.heartbeat_at <= clock_timestamp()-interval '10 seconds' THEN 'offline'
                 WHEN b.id IS NOT NULL THEN 'busy' ELSE 'idle' END AS status
            FROM workers w LEFT JOIN bookings b ON b.id=w.current_booking_id AND b.worker_id=w.id
                AND b.status='processing' AND b.lease_until > clock_timestamp()
            ORDER BY (w.heartbeat_at > clock_timestamp()-interval '10 seconds') DESC,w.started_at DESC LIMIT 24""").fetchall()
        totals = conn.execute("""SELECT count(*) AS total_bookings,
            count(*) FILTER(WHERE status='pending') AS pending,
            count(*) FILTER(WHERE status='processing') AS processing,
            count(*) FILTER(WHERE status='confirmed') AS confirmed,
            count(*) FILTER(WHERE status='rejected') AS rejected,
            COALESCE(sum(quantity) FILTER(WHERE status='confirmed'),0) AS confirmed_tickets,
            COALESCE(avg(extract(epoch FROM (finished_at-started_at))*1000)
                FILTER(WHERE status IN ('confirmed','rejected')),0)::float AS average_processing_ms
            FROM bookings b JOIN events e ON e.id=b.event_id WHERE NOT e.is_demo OR %s""",
                              (include_demo,)).fetchone()
        totals["queue_depth"] = totals["pending"] + totals["processing"]
        totals["active_workers"] = sum(worker["online"] for worker in workers)
        activity = conn.execute("""SELECT a.* FROM booking_activity a
            JOIN bookings b ON b.id=a.booking_id JOIN events e ON e.id=b.event_id
            WHERE NOT e.is_demo OR %s ORDER BY a.id DESC LIMIT 20""", (include_demo,)).fetchall()
        return {"events": events, "bookings": bookings, "workers": workers, "metrics": totals, "activity": activity,
                "coordination": coordination_snapshot(conn),
                "server_time": datetime.now(timezone.utc),
                "config": {"processing_seconds": db.PROCESSING_SECONDS, "lease_seconds": db.LEASE_SECONDS}}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="web")
