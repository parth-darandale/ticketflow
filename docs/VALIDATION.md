# Validation on 21 October 2026

TicketFlow was built and deployed on this PC with Docker Desktop using Linux containers. The deployment has one API, one PostgreSQL database and three worker replicas, with the browser served at `http://127.0.0.1:8088`.

## Clocks and election extension - 21 October 2026

The upgraded application passed **44 automated checks**: 9 pure Lamport tests, 6 proof/cleanup tests, 6 live HTTP booking tests, 4 live HTTP trace/coordination tests, 15 isolated-schema PostgreSQL election/trace invariants, and 4 queue invariants with workers paused. The final host suite of 25 tests completed in 29.209 seconds; the 15 isolated database tests in 3.691 seconds and queue suite in 0.724 seconds. Python compilation, JavaScript syntax and Git whitespace checks also passed.

The controlled Docker proof used `docker stop --signal SIGKILL --timeout 0` on the elected worker while it owned a booking. The stopped worker remained stopped, and no graceful release event occurred for its failed epoch. Worker `83f64dbb0b2a` at epoch 9 was replaced by `439425c55355` at epoch 10. The successor was observed 6.281 seconds after the stop completed. Booking `cb031dba-d049-40c6-8a3d-45f5e2ce6df6` completed on attempt 2 in 14.625 seconds after the stop; exactly one ticket was deducted from three seats. The recovery activity was tied to the recorded successor's process-run ID. Three unpaused, healthy workers and an idle queue were restored.

The recorded causal chain was queued L41, claimed L42, recovered L43, claimed L44, confirmed L45, observed L46. Receive timestamps match the previous booking message and advance the receiving process's clock. The API observation is response-only. Raw proof: `docs/evidence/syllabus.json`.

Existing showcase bookings and inventory were compared with the pre-upgrade snapshot and preserved. A database backup was kept locally before the additive migration. Browser checks verified current leader/epoch/history, normal four-event logical traces, recovered six-event traces, and historical NULL/Uninstrumented events. The default browser viewport had no horizontal page overflow and no JavaScript errors. Asset versioning prevents stale scripts hiding the new controls. Actual captures of clocks and election are in `docs/assets/submission/`.

The following sections retain the earlier baseline measurements. They predate the clock/election extension and are not new timing claims.

## Correctness

Six HTTP integration tests passed in 21.998 seconds against the real Docker deployment:

- Eighteen concurrent one-ticket requests against nine seats produced nine confirmations and nine rejections, with zero remaining seats.
- Twelve concurrent submissions using one idempotency key produced one booking and one inventory debit. A later retry returned the same completed booking.
- Reusing an idempotency key with a different payload returned HTTP 409.
- Two requests for three seats against five seats produced one confirmation and one rejection; a subsequent two-ticket booking consumed the remaining capacity.
- Invalid quantities, missing request keys and empty aliases were rejected without creating bookings.
- Generated test events were excluded from the showcase and available through the explicit demo view.

Four direct PostgreSQL invariant tests also passed in 0.690 seconds, with workers paused:

- An expired or superseded claim could not deduct inventory; the replacement claim finalized once, even when completion was repeated.
- A stale heartbeat could not renew a newer claim's lease.
- An injected failure after inventory mutation rolled back both inventory and booking state.
- A delayed heartbeat could not restore an already-completed worker assignment.

The browser was used to book two tickets for Design Futures. The request progressed from pending to confirmed, and availability changed from 40 to 38.

The Windows `Stop-App.cmd` and `Start-App.cmd -NoBrowser` launchers were exercised end to end. All TicketFlow containers were removed and recreated; the confirmed two-ticket booking and its 38 remaining seats persisted in the named volume. All five containers became healthy again. The final API validation and demo-isolation checks passed after rebuilding, including rejection of whitespace-only demo titles. The browser was reloaded against the rebuilt app and showed the preserved confirmation, three online workers and no horizontal page overflow at its 498-pixel viewport.

## Worker-crash recovery

The controlled recovery experiment stopped worker container `0238501d4eed` while its booking was processing and left it stopped. Another worker, `fd53fb85764c`, reclaimed the expired lease and confirmed the booking on attempt 2.

- Recovery from interruption to completion: **13.750 seconds**.
- Event capacity: 3; tickets confirmed: 1; remaining: 2.
- Final worker ID differed from the stopped worker ID.
- Three workers were restored after the experiment.

Raw result: `artifacts/recovery.json`; trace: `artifacts/recovery.log`.

## Measured scaling

Each phase used a separate event and 12 concurrent one-ticket booking requests. Every booking was confirmed, each was attempted once, and the expected worker IDs were observed handling work. Docker scaling and startup were excluded from the timing.

| Workers | Completed bookings | Elapsed | Throughput |
| --- | --- | --- | --- |
| 1 | 12 / 12 | 24.781 s | 0.4842 bookings/s |
| 3 | 12 / 12 | 8.672 s | 1.3838 bookings/s |

Observed elapsed-time speedup: **2.858×**. The workload includes the configured **two-second simulated validation delay per booking**. This is a local measurement of this workload, including HTTP, database and polling costs; it does not establish CPU speedup, linear scaling or resilience to host/database failure.

Raw result: `artifacts/benchmark.json`; trace: `artifacts/benchmark.log`. The benchmark script records runtime configuration on subsequent runs and supports repeatable fresh fixtures.

## Repeat the checks

Follow `README.md` and `docs/DEMO.md`. The verification and benchmark scripts create isolated demo events and preserve the featured-event inventory. Worker recovery and benchmark scripts restore three replicas in a `finally` block. The database tests require an idle queue and paused workers.

Generated runs live in ignored `artifacts/`; tracked measurement copies in `docs/evidence/` are included in the submission ZIP alongside this guide.
