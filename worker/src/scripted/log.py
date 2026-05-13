# worker/src/scripted/log.py
"""Step logger.

Two layers:
  - Live (Redis, TTL'd): RPUSH each entry to job:<id>:steps. Lets the API
    /api/jobs/:id endpoint stream live progress to a debug UI.
  - Persistent: every entry is also accumulated in-memory; runner returns
    them inside RunOutcome.run_log, run_job.py forwards them in
    notify_job_completed.runLog, the API persists to Firestore under
    borderTaxRequests/<rid>/agentAutomationLog.

Logging never raises -- Redis hiccups print but don't break the run.
"""

from __future__ import annotations

import redis

from .types import StepLog


class StepLogger:
    def __init__(
        self,
        job_id: str,
        r: redis.Redis,
        ttl_seconds: int = 60 * 60 * 24,
    ) -> None:
        self.job_id = job_id
        self.r = r
        self.ttl = ttl_seconds
        self.entries: list[StepLog] = []
        self._counter = 0

    def next_index(self) -> int:
        i = self._counter
        self._counter += 1
        return i

    def record(self, entry: StepLog) -> None:
        self.entries.append(entry)
        try:
            self.r.rpush(f"job:{self.job_id}:steps", entry.model_dump_json())
            self.r.expire(f"job:{self.job_id}:steps", self.ttl)
        except Exception as e:
            print(f"[{self.job_id}] StepLogger.record Redis error: {e}")

    def total_handoff_cost(self) -> float:
        return sum(e.handoff_cost_usd or 0.0 for e in self.entries)

    def dump(self) -> list[StepLog]:
        """In-memory copy, used by the runner to fill RunOutcome.run_log."""
        return list(self.entries)
