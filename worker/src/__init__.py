# worker/src/scripted/__init__.py
"""Scripted automation runner.

A lightweight, deterministic step engine for tasks where AI is overkill on
most pages (border tax, etc.). The runner walks plain imperative Python that
calls a small set of step primitives. Each step records a StepLog. When a
step genuinely can't proceed, it raises HandoffNeeded and the runner spins
up a scoped AI Agent on the same BrowserSession for a narrow goal.

Status mapping to existing job lifecycle:
    RunOutcome(status="done")    -> job status "done"
    RunOutcome(status="partial") -> job status "partial"
    RunOutcome(status="failed")  -> job status "failed"
"""
