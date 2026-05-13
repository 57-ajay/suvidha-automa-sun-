# worker/src/scripted/handoff.py
"""AI rescue helper.

When a scripted step raises HandoffNeeded, the runner spins up a tightly
scoped browser-use Agent on the SAME BrowserSession. The Agent gets:
  - A narrow goal (one sentence).
  - The reason the script gave up.
  - Optional context hint about the current page.
  - A hard ceiling of 15 steps (per product decision).

The Agent shares cookies, current tab, scroll position -- it just picks up
where the script left off, finishes the narrow task, and calls done.
Returns (summary, cost_usd) so the caller can record both into the StepLog.
"""

from __future__ import annotations

from browser_use import Agent, ChatGoogle, Tools

from cost_calculator import fill_missing_cost


# Centralized so captcha.py and runner.py share the same model/project.
LLM_MODEL = "gemini-3-flash-preview"
VERTEX_PROJECT = "nomadic-bison-481114-s4"
MAX_RESCUE_STEPS = 15


def build_llm():
    """Single source of truth for the LLM client used by handoff + captcha."""
    return ChatGoogle(
        model=LLM_MODEL,
        vertexai=True,
        project=VERTEX_PROJECT,
    )


async def run_ai_rescue(
    session,
    *,
    goal: str,
    reason: str,
    page_context_hint: str | None = None,
    max_steps: int = MAX_RESCUE_STEPS,
) -> tuple[str, float]:
    """Hand the live session to a scoped Agent.

    Returns (summary, cost_usd). cost_usd is 0.0 if usage extraction fails.
    The caller is responsible for recording these into the step log.
    """

    prompt = (
        "You are taking over a partially-completed automation. A scripted "
        "runner was driving the browser and gave up at this point.\n\n"
        f"REASON THE SCRIPT GAVE UP: {reason}\n\n"
        f"YOUR GOAL -- AND THE ONLY THING YOU SHOULD DO -- IS: {goal}\n\n"
        f"PAGE CONTEXT: {
            page_context_hint or 'Inspect the current page and proceed.'
        }\n\n"
        "RULES:\n"
        " - Do NOT navigate away from the current site unless the goal explicitly says to.\n"
        " - Do NOT explore unrelated pages, dropdowns, or 'try other approaches'.\n"
        " - Use as few steps as possible.\n"
        f" - You have at most {max_steps} steps before you are stopped.\n"
        " - When the goal is achieved (or you've determined it is impossible), call `done` "
        "with a one-paragraph summary of what you did."
    )

    agent = Agent(
        task=prompt,
        llm=build_llm(),
        browser=session,
        tools=Tools(),
        calculate_cost=True,
    )

    result = await agent.run(max_steps=max_steps)
    summary = result.final_result() or "Rescue agent returned no summary."

    cost_usd = 0.0
    try:
        usage_summary = await agent.token_cost_service.get_usage_summary()
        cost_data = fill_missing_cost(usage_summary)
        if cost_data:
            cost_usd = float(cost_data.get("totalCost", 0.0) or 0.0)
    except Exception as e:
        print(f"[handoff] cost extraction failed: {e}")

    return summary, cost_usd
