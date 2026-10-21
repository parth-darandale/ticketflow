# Distributed-systems concepts in TicketFlow

## Implemented additions

| Topic | Actual implementation | Demonstration |
| --- | --- | --- |
| Lamport logical clocks | One locked counter per API/worker process incarnation; timestamp propagation through queued bookings and recorded worker outcomes; receive rule `max(local, remote) + 1`. | Select **View trace** on a new booking. Observe queue/send, worker receive, completion/send and API receive. |
| Coordinator election and failure recovery | Workers contend for one database lease. Acquiring an expired lease increments the epoch; only its current owner can recover expired booking claims. | Run `python scripts/verify_syllabus.py`; a different worker takes over after the elected worker is stopped. |
| Synchronization / mutual exclusion | PostgreSQL locks protect job claims, inventory and the leader's recovery transaction. | Concurrent requests never reserve more than event capacity. |
| Asynchronous communication | API and workers exchange durable booking messages through PostgreSQL over the Docker network. | Booking acceptance and final confirmation are separate steps. |
| Scalability | Independent worker replicas share pending jobs. | Compare one and three workers using `scripts/benchmark.py`. |

The lab manual's logical-clock exercise uses Lamport rules; this project applies those rules to actual processes. The lab manual also teaches Bully and Ring election algorithms. TicketFlow demonstrates the broader leader-election concept using a PostgreSQL lease, and does **not** claim to implement either named algorithm.

## Clock model

`app/clock.py` supplies one clock shared by a process's request/heartbeat threads, never a counter shared by all containers. `PROCESS_ID` includes a new UUID per process incarnation, so a restarted process can start at zero without being confused with its previous incarnation. `app/db.py` writes message timestamps and activity in the same transaction as each booking transition. Transaction rollbacks may leave unused counter values; gaps are valid.

The clock instruments booking workflow messages, including leader requeueing. It is not instrumentation of every SQL packet or heartbeat. API observations are returned in the trace response without writing a new activity row on every poll. Physical timestamps and lease deadlines use the database clock separately. Historical events remain NULL/Uninstrumented. The trace preserves known message dependencies; comparing timestamps of unrelated events cannot establish causality.

## Election and fencing model

`app/leader.py` uses a singleton lease row containing worker ID, process-run owner ID, epoch and deadline. A row lock serializes contenders. An unexpired holder may renew without changing the epoch; an expired acquisition always advances it, even for the same process. Every recovery transaction locks that row and validates the owner, epoch and live lease before locking expired bookings. The lock remains held through commit, serializing recovery with takeover. Recovery clears old job ownership and publishes a pending job; it never deducts inventory.

The leader runs its coordination loop while also processing ordinary bookings. Its role is recovery housekeeping, not assigning customers to workers. Worker consumers take pending jobs independently. Missing heartbeats suggest unavailability; they do not prove a process has crashed. A database outage prevents election and booking progress until database connectivity returns. This is not quorum consensus, multi-machine high availability or a replicated database.

## Verification

- Pure tests cover receive rules, invalid timestamps, thread safety and incarnation identity.
- HTTP tests check independent causal traces for concurrent bookings, idempotent retries, read-only trace polling and live election status.
- PostgreSQL tests use disposable schemas to check concurrent election, lease renewal, epochs, stale-owner rejection, atomic recovery and rollback.
- The controlled Docker proof stops the elected worker while it owns a booking; it records successor identity, higher epoch, recovery trace and exactly one reservation effect, then restores three workers.

Recorded experiment: `docs/evidence/syllabus.json`. Re-run with `python scripts/verify_syllabus.py --output artifacts/syllabus.json`.

## Primary references

- Leslie Lamport, [Time, Clocks, and the Ordering of Events in a Distributed System (1978)](https://www.microsoft.com/en-us/research/publication/time-clocks-ordering-events-distributed-system/).
- PostgreSQL 17, [Explicit Locking](https://www.postgresql.org/docs/17/explicit-locking.html).
- PostgreSQL 17, [SELECT locking clauses and SKIP LOCKED](https://www.postgresql.org/docs/17/sql-select.html).
