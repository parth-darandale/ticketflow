# TicketFlow demonstration

Run commands from the TicketFlow repository folder. Start Docker Desktop with Linux containers first. Use a separate test event for each experiment so earlier bookings do not change the result.

## 1. Start and inspect

```powershell
.\Start-App.cmd
docker compose ps
```

Open [http://127.0.0.1:8088](http://127.0.0.1:8088), or the configured `PORT`. The default configuration has one API, one PostgreSQL service and three independent workers. Submit a booking in the UI and watch it move through `pending`, `processing` and a terminal state. The exact time spent in each state depends on the running workload.

Explain: the HTTP request queues work; workers perform the seat reservation separately. A worker restart does not erase the queued booking.

## 2. Verify no overselling and idempotency

```powershell
python scripts/verify_system.py
```

This creates fresh fixture events and checks the system's invariants. The important observations are:

- Requests exceed the fixture's capacity; successful reservations cannot exceed the available seats.
- Confirmed ticket quantities agree with the inventory reduction, and inventory stays nonnegative.
- Repeating the same UUID idempotency key with the same body identifies the same booking.
- Reusing that key for a different body returns HTTP 409.

Use the script's pass/fail output as evidence. Do not infer correctness merely from the dashboard animation or a count of HTTP responses: an accepted booking still needs a terminal result.

For an individual API request, open `/docs`. Create a hidden fixture with `POST /api/demo/events`, using a body such as:

```json
{"title": "Viva inventory experiment", "total_seats": 5}
```

Copy the returned event ID, then submit bookings through `POST /api/bookings` with a fresh UUID `Idempotency-Key`:

```json
{"event_id": "COPY_THE_EVENT_ID", "customer_name": "Demo Student", "quantity": 2}
```

Use the actual schema shown in `/docs` for the event-ID type. Inspect each booking with `GET /api/bookings/{id}` and the final inventory with `GET /api/events/{id}`. Hidden fixture events avoid cluttering the normal event list.

## 3. Demonstrate worker-crash recovery

```powershell
python scripts/verify_system.py --recovery
```

The recovery scenario interrupts a worker handling a fixture booking and verifies that work can complete after its ownership expires. The lease lasts 12 seconds and is renewed every 2 seconds while the worker remains active. Recovery requires waiting for lease expiry and another worker's claim; it is not instantaneous.

Explain: the queue row survives because it is stored in PostgreSQL. An expired lease allows a retry, and a changed fencing token prevents the previous owner from finalizing an outdated claim. Inventory and terminal booking status commit together, so recovery must not reserve the same booking's seats twice.

The script restores three workers. A failure should be investigated through its output and service logs:

```powershell
docker compose ps
docker compose logs --tail 100 worker api
```

## 4. Measure one worker versus three

```powershell
python scripts/benchmark.py --orders 12 --output artifacts/benchmark.json
```

The benchmark runs the workload with one worker and with three workers, records measured completion times, and restores three workers. Read `artifacts/benchmark.json` for the actual result from this machine. Omit `--output` to keep each run in a separate timestamped JSON file. Do not present a theoretical threefold speedup as an observation: scheduling, database contention and startup timing affect the measurement.

To change the worker count manually:

```powershell
.\Start-App.cmd -Workers 1 -NoBrowser
.\Start-App.cmd -Workers 3 -NoBrowser
```

Point out that adding workers changes processing capacity without changing the API address or replacing the database. More workers do not increase an event's seat inventory.

## 5. Stop and prove persistence

```powershell
.\Stop-App.cmd
.\Start-App.cmd
```

Reopen the dashboard and confirm previous bookings remain. `Stop-App.cmd` uses `docker compose down` without deleting the named database volume. After a PC reboot, start Docker Desktop before running `Start-App.cmd`.

## Short viva answers

**Why PostgreSQL instead of an in-memory queue?** Accepted jobs, inventory and booking states survive worker restarts. One database transaction can protect both the seat change and the final booking result.

**Why `SKIP LOCKED`?** Concurrent workers can claim different jobs while skipping rows another worker has already locked.

**Why leases and fencing tokens?** Leases let work recover after a crash. Fencing tokens reject finalization by an old worker whose ownership expired and was replaced.

**Is this exactly-once processing?** Jobs can be attempted again. Idempotency, ownership checks and atomic finalization guard the booking effect against duplicate reservation.

**What still fails if the PC or database stops?** All containers share one host, and PostgreSQL has no replica or automatic failover. The project demonstrates worker-level recovery and concurrency, not host-level high availability.

**Does it take money or sell real tickets?** No. This is a local reservation and distributed-systems demonstration.
