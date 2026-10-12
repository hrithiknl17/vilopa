# 0001. Postgres is the job queue

- Status: accepted
- Date: 2026-09-30

## Context

mita runs erasure work in the background: plan, execute per target, verify. Jobs must not be
lost if a worker crashes, and a request's state change must never commit without its first
job, or the other way round.

The target users self-host mita at small companies. Every extra piece of infrastructure is
one more thing they must run, back up and secure. Expected volume is low: thousands of tasks
a day at most.

## Decision

Use a `jobs` table in mita's own Postgres as the queue. Workers claim jobs with
`FOR UPDATE SKIP LOCKED`, hold a lease with an expiry, and every write-back is fenced by a
lease token. A request row and its first job are inserted in the same transaction.

## Consequences

- Transactional enqueue removes the "state saved, job lost" class of bugs.
- One stateful dependency for self-hosters: mita plus Postgres, no Redis.
- Costs: queue-table bloat and vacuum pressure, and a throughput ceiling far above the
  expected load. Revisit if sustained volume passes roughly 1,000 jobs per second, or if a
  user needs cross-region workers.
- Needs its own tests: claim contention, lease expiry, fencing, reaping (week 7).

## Rejected

- **Redis + a worker library (Celery, RQ, BullMQ):** a second system to run and no
  transactional enqueue with request state.
- **FastAPI background tasks:** lost on restart, no retries, no visibility.
