"""PostgreSQL lease election; the elected worker recovers abandoned bookings.

This is database-mediated coordination, not a consensus protocol. Every leader
operation locks the singleton lease row before booking and worker rows. Holding
that lock through commit fences an old epoch against a concurrent takeover.
"""

from . import db
from .clock import CLOCK, PROCESS_ID

LEASE_SECONDS = 8
RECOVERY_BATCH_SIZE = 100


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS cluster_leader (
        singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
        worker_id text,
        owner_id text,
        epoch bigint NOT NULL DEFAULT 0 CHECK (epoch >= 0),
        lease_until timestamptz,
        recovered_total bigint NOT NULL DEFAULT 0 CHECK (recovered_total >= 0)
    );
    INSERT INTO cluster_leader(singleton) VALUES (true) ON CONFLICT DO NOTHING;
    CREATE TABLE IF NOT EXISTS leader_history (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        worker_id text NOT NULL,
        owner_id text NOT NULL,
        epoch bigint NOT NULL,
        kind text NOT NULL CHECK (kind IN ('elected','released')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        recovered_count bigint NOT NULL DEFAULT 0
    );""")


def _locked_lease(conn):
    current = conn.execute("SELECT * FROM cluster_leader WHERE singleton FOR UPDATE").fetchone()
    if current:
        # Evaluate time after acquiring the lock, rather than before a lock wait.
        now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        current["active"] = bool(current["lease_until"] and current["lease_until"] > now)
    return current


def acquire_or_renew(conn, worker_id, owner_id, *, expected_epoch=None):
    """Return a held lease, or None when a different owner/epoch remains active.

    Supplying an expected epoch is a renewal only: an expired lease cannot be
    resurrected with that epoch. A fresh election must omit expected_epoch.
    """
    current = _locked_lease(conn)
    if current is None:
        raise RuntimeError("Leader schema has not been initialized.")
    same_owner = current["worker_id"] == worker_id and current["owner_id"] == owner_id
    if expected_epoch is not None and (
            not current["active"] or not same_owner or current["epoch"] != expected_epoch):
        return None
    if current["active"] and not same_owner:
        return None
    elected = not current["active"]
    epoch = current["epoch"] + 1 if elected else current["epoch"]
    result = conn.execute("""UPDATE cluster_leader SET worker_id=%s,owner_id=%s,epoch=%s,
        lease_until=clock_timestamp()+(%s * interval '1 second')
        WHERE singleton RETURNING *""", (worker_id, owner_id, epoch, LEASE_SECONDS)).fetchone()
    if elected:
        conn.execute("""INSERT INTO leader_history(worker_id,owner_id,epoch,kind)
            VALUES (%s,%s,%s,'elected')""", (worker_id, owner_id, epoch))
    return result


def recover_expired(conn, worker_id, owner_id, epoch):
    """Requeue a bounded batch, only while this transaction owns the live epoch.

    The lease lock remains held until the caller commits or rolls back. Recovery
    changes ownership/state only; reservation inventory is never touched here.
    """
    current = _locked_lease(conn)
    if not current or not current["active"] or (
            current["worker_id"], current["owner_id"], current["epoch"]) != (worker_id, owner_id, epoch):
        return 0
    expired = conn.execute("""SELECT id,worker_id,last_logical_time FROM bookings
        WHERE status='processing' AND lease_until < clock_timestamp()
        ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT %s""", (RECOVERY_BATCH_SIZE,)).fetchall()
    for booking in expired:
        received_time = booking["last_logical_time"]
        logical_time = CLOCK.receive(received_time or 0)
        conn.execute("""UPDATE bookings SET status='pending',worker_id=NULL,claim_token=NULL,
            lease_until=NULL,last_logical_time=%s WHERE id=%s""", (logical_time, booking["id"]))
        conn.execute("""UPDATE workers SET current_booking_id=NULL
            WHERE id=%s AND current_booking_id=%s""", (booking["worker_id"], booking["id"]))
        db.activity(conn, booking["id"], "recovered",
                    f"Leader requeued an expired booking (epoch {epoch}); previous worker {booking['worker_id']}",
                    worker_id, logical_time=logical_time, received_time=received_time, process_id=owner_id)
    count = len(expired)
    if count:
        conn.execute("UPDATE cluster_leader SET recovered_total=recovered_total+%s WHERE singleton", (count,))
        conn.execute("""UPDATE leader_history SET recovered_count=recovered_count+%s
            WHERE owner_id=%s AND epoch=%s AND kind='elected'""", (count, owner_id, epoch))
    return count


def snapshot(conn):
    current = conn.execute("""SELECT worker_id,owner_id,epoch,lease_until,recovered_total,
        COALESCE(lease_until > clock_timestamp(),false) AS active
        FROM cluster_leader WHERE singleton""").fetchone()
    result = dict(current or {"worker_id": None, "owner_id": None, "epoch": 0,
                              "lease_until": None, "active": False, "recovered_total": 0})
    result["lease_seconds"] = LEASE_SECONDS
    result["history"] = conn.execute("""SELECT id,worker_id,owner_id,epoch,kind,created_at,recovered_count
        FROM leader_history ORDER BY id DESC LIMIT 20""").fetchall()
    return result


def step(worker_id, owner_id=PROCESS_ID):
    """Contend/renew and perform the elected worker's useful recovery duty."""
    with db.connect() as conn:
        # Keep lease contention and maintenance bounded; no external calls occur
        # while holding locks. Connection establishment has db.connect's timeout.
        conn.execute("SET LOCAL statement_timeout='3s'")
        conn.execute("SET LOCAL lock_timeout='1s'")
        lease = acquire_or_renew(conn, worker_id, owner_id)
        recovered = recover_expired(conn, worker_id, owner_id, lease["epoch"]) if lease else 0
        result = snapshot(conn)
        result["recovered_count"] = recovered
        result["is_leader"] = bool(result["active"] and result["worker_id"] == worker_id
                                   and result["owner_id"] == owner_id)
        return result


def release(worker_id, owner_id=PROCESS_ID, epoch=None):
    """Release only this process's still-current lease, optionally fencing epoch."""
    with db.connect() as conn:
        conn.execute("SET LOCAL statement_timeout='3s'")
        conn.execute("SET LOCAL lock_timeout='1s'")
        current = _locked_lease(conn)
        if not current or not current["active"] or (
                current["worker_id"], current["owner_id"]) != (worker_id, owner_id):
            return False
        if epoch is not None and current["epoch"] != epoch:
            return False
        conn.execute("UPDATE cluster_leader SET lease_until=clock_timestamp() WHERE singleton")
        conn.execute("""INSERT INTO leader_history(worker_id,owner_id,epoch,kind)
            VALUES (%s,%s,%s,'released')""", (worker_id, owner_id, current["epoch"]))
        return True
