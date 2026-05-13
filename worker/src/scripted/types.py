# worker/src/scripted/types.py
"""Core types for the scripted runner.

Status, log entries, run outcomes, and the two exceptions the runner watches
for. No external deps besides pydantic.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class StepStatus(str, Enum):
    OK = "ok"
    RETRIED = "retried"
    HANDED_OFF = "handed_off"
    FAILED = "failed"
    SKIPPED = "skipped"


class StepLog(BaseModel):
    """One entry per step attempt.

    Written to Redis live (job:<id>:steps list) by StepLogger.record so the
    /api/jobs/:id endpoint can stream them for a debug UI. Also accumulated
    in-memory and included in the notify_job_completed payload for Firestore
    persistence under borderTaxRequests/<rid>/agentAutomationLog.
    """

    index: int
    name: str
    status: StepStatus
    url: str | None = None
    selector: str | None = None
    value: str | None = None
    attempt: int = 1
    duration_ms: int = 0
    error: str | None = None
    handoff_reason: str | None = None
    handoff_summary: str | None = None
    handoff_cost_usd: float | None = None
    started_at: str = Field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))


class RunOutcome(BaseModel):
    """Final result of a scripted run. run_job.py maps this to the existing
    job lifecycle (done | partial | failed) used by notify_job_completed."""

    status: Literal["done", "partial", "failed"]
    summary: str
    abort_reason: str | None = None
    partial_reasons: list[str] = []
    receipt_number: str | None = None
    amount: float | None = None
    total_cost_usd: float = 0.0
    run_log: list[StepLog] = []


class ScriptedAbort(Exception):
    """Clean abort with a user-visible reason (vehicle ineligible, validity
    popup, etc.). Caught by the runner -> status='failed' with abort_reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class HandoffNeeded(Exception):
    """A scripted step gave up; AI agent should take over for a narrow goal.
    Caught by the runner, which calls handoff.run_ai_rescue. The rescue
    returns either a summary (success/partial) or exhausts its step budget
    (-> partial with reason 'ai_rescue_exhausted')."""

    def __init__(self, reason: str, goal: str, scope: str = "rescue") -> None:
        super().__init__(reason)
        self.reason = reason
        self.goal = goal
        self.scope = scope  # "captcha" | "rescue" | "receipt_read" | ...
