# worker/src/scripted/captcha.py
"""Canvas-captcha solver.

Flow:
  1. Read the <canvas> as a base64 PNG via CDP (canvas.toDataURL).
  2. Send the image to the LLM with a tight OCR prompt.
  3. Fill the captcha input.
  4. Caller-provided submit_action() advances the form and returns whether
     the form actually moved past the captcha page.
  5. On failure, optionally click refresh_selector to regenerate the captcha
     and retry. Max max_ai_attempts attempts (default 5).
  6. After exhausting AI attempts:
       - source != 'app':  call wait_for_human(reason=...) so a human can
         type the captcha through the live-VNC UI.
       - source == 'app':  raise ScriptedAbort.

Two LLM-invocation paths are supported because the v0.12 ChatGoogle API
surface has shifted across recent releases:
  - Preferred: direct llm.ainvoke([UserMessage(image+text)]) -- cheap, one
    call per attempt.
  - Fallback: 1-step Agent run, which works on whatever message schema the
    installed version accepts but costs a few extra tokens for the loop.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Awaitable, Callable

import redis
from browser_use import Agent, Tools

from .handoff import build_llm
from .log import StepLogger
from .steps import _cdp_eval, _wait_visible, fill, click_by_text, click
from .types import ScriptedAbort, StepLog, StepStatus


SubmitAction = Callable[[], Awaitable[bool]]
"""Caller-supplied callable. Returns True if the form advanced past the
captcha (i.e. captcha was accepted), False if the form is still on the
captcha page (captcha was rejected)."""


async def _read_canvas_png_b64(session, canvas_selector: str) -> str | None:
    """Return base64-encoded PNG of the canvas, or None if not present."""
    expr = (
        "(function(s){var c=document.querySelector(s);"
        "if(!c) return null;"
        "if(!(c instanceof HTMLCanvasElement)) return null;"
        "try { return c.toDataURL('image/png').replace(/^data:image\\/png;base64,/, ''); }"
        "catch(e) { return null; }"
        "})(" + json.dumps(canvas_selector) + ")"
    )
    return await _cdp_eval(session, expr)


async def _ocr_via_llm(image_b64: str) -> tuple[str, float]:
    """Direct LLM call for OCR. Returns (text, cost_usd).

    NOTE: The exact ChatGoogle.ainvoke message schema can differ across
    browser-use 0.12.x patch releases. We try the most common shapes and
    fall through to a sentinel 'UNREADABLE' if all fail, letting the caller
    refresh + retry. The fallback path in solve_canvas_captcha covers cases
    where this entire helper returns UNREADABLE repeatedly.
    """
    llm = build_llm()

    # browser-use's ChatGoogle.ainvoke expects message OBJECTS (it calls
    # .model_copy() on them), not raw dicts — passing dicts raises
    # "'dict' object has no attribute 'model_copy'". Build the proper
    # UserMessage with a text part + an inline image part.
    prompt_text = (
        "This is a captcha image from a government website. "
        "Read the characters and reply with ONLY those characters, "
        "no spaces, no quotes, no explanation. "
        "If you cannot read it clearly, reply with the single word: UNREADABLE"
    )
    try:
        from browser_use.llm.messages import (
            UserMessage,
            ContentPartTextParam,
            ContentPartImageParam,
            ImageURL,
        )

        msg = UserMessage(
            content=[
                ContentPartTextParam(text=prompt_text),
                ContentPartImageParam(
                    image_url=ImageURL(
                        url=f"data:image/png;base64,{image_b64}",
                        media_type="image/png",
                        detail="auto",
                    )
                ),
            ]
        )
        response = await llm.ainvoke([msg])
        text = (
            getattr(response, "completion", None)
            or getattr(response, "content", None)
            or ""
        )
        if isinstance(text, list):
            # Some message-content shapes return a list of parts
            text = " ".join(
                str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in text
            )
        text = (text or "").strip()
        if text:
            return text, 0.0
    except Exception as e:
        print(f"[captcha] llm.ainvoke failed: {e}")

    return "UNREADABLE", 0.0


async def _ocr_via_agent(session) -> tuple[str, float]:
    """Fallback OCR via a 1-step Agent that screenshots the page and writes
    the captcha into a captured tool call. Used only if _ocr_via_llm
    couldn't be wired up against the installed browser-use version."""
    captured: dict[str, str] = {"text": ""}
    tools = Tools()

    @tools.action(
        description=(
            "Submit the captcha text you read from the captcha image on the "
            "current page. Pass the characters as a single string with no "
            "spaces. If you cannot read them, pass 'UNREADABLE'."
        )
    )
    async def submit_captcha(text: str) -> str:
        captured["text"] = (text or "").strip()
        return "ok"

    prompt = (
        "Look ONLY at the captcha image on the current page (a small canvas "
        "with distorted characters, near a 'Pay Online' button). Read the "
        "characters. Call submit_captcha with ONLY those characters, no "
        "spaces. Then call done. Do NOT click or type anything else."
    )

    agent = Agent(
        task=prompt,
        llm=build_llm(),
        browser=session,
        tools=tools,
        calculate_cost=True,
    )
    result = await agent.run(max_steps=3)
    cost_usd = 0.0
    try:
        from cost_calculator import fill_missing_cost

        usage = await agent.token_cost_service.get_usage_summary()
        cd = fill_missing_cost(usage)
        if cd:
            cost_usd = float(cd.get("totalCost", 0.0) or 0.0)
    except Exception:
        pass

    return captured["text"] or "UNREADABLE", cost_usd


async def _wait_for_human_via_redis(
    job_id: str,
    r: redis.Redis,
    reason: str,
    *,
    timeout: int = 200,
) -> str:
    """Same Redis-flag mechanism used by agent.py's wait_for_human tool.
    Lifted here so the scripted runner doesn't need agent.py imports."""
    JOB_TTL = 60 * 60 * 24
    r.hset(
        f"job:{job_id}",
        mapping={"status": "waiting_for_human", "waitReason": reason},
    )
    waited = 0
    while waited < timeout:
        human_input = r.hget(f"job:{job_id}", "humanInput")
        if human_input:
            text = (
                human_input.decode() if isinstance(human_input, bytes) else human_input
            )
            r.hdel(f"job:{job_id}", "humanInput", "waitReason")
            r.hset(f"job:{job_id}", "status", "running")
            return text
        await asyncio.sleep(1)
        waited += 1
    r.hdel(f"job:{job_id}", "waitReason")
    r.hset(f"job:{job_id}", "status", "running")
    r.rpush(f"job:{job_id}:partial_reasons", f"human_timeout:{reason}")
    r.expire(f"job:{job_id}:partial_reasons", JOB_TTL)
    return ""


async def solve_canvas_captcha(
    session,
    *,
    canvas_selector: str,
    input_selector: str,
    refresh_selector: str | None,
    submit_action: SubmitAction,
    job_id: str,
    r: redis.Redis,
    source: str,
    log: StepLogger,
    name: str = "solve_captcha",
    max_ai_attempts: int = 5,
) -> None:
    """Solve the captcha at canvas_selector. Raises ScriptedAbort if exhausted.

    The caller is responsible for everything around the captcha:
      - Filling other form fields BEFORE calling this.
      - Providing submit_action: clicks Pay Online / Submit and returns whether
        the form advanced past the captcha page.
      - Continuing the flow AFTER this returns successfully.
    """
    total_cost = 0.0

    for attempt in range(1, max_ai_attempts + 1):
        started = time.monotonic()
        try:
            await _wait_visible(session, canvas_selector, timeout=10)
            b64 = await _read_canvas_png_b64(session, canvas_selector)
            if not b64:
                raise RuntimeError("canvas could not be read as PNG")

            text, cost = await _ocr_via_llm(b64)
            if not text or text.upper() == "UNREADABLE":
                text2, cost2 = await _ocr_via_agent(session)
                text = text2
                cost += cost2
            total_cost += cost

            if not text or text.upper() == "UNREADABLE":
                raise RuntimeError("LLM could not read captcha")

            await fill(
                session,
                input_selector,
                text,
                log=log,
                name=f"{name}.fill_attempt_{attempt}",
            )

            advanced = await submit_action()
            if advanced:
                log.record(
                    StepLog(
                        index=log.next_index(),
                        name=name,
                        status=StepStatus.OK,
                        attempt=attempt,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        value=text,
                        handoff_reason="captcha_ocr",
                        handoff_cost_usd=total_cost,
                    )
                )
                return

            # Captcha rejected. Refresh if we can, then retry.
            if refresh_selector and attempt < max_ai_attempts:
                try:
                    await click(
                        session,
                        refresh_selector,
                        log=log,
                        name=f"{name}.refresh_attempt_{attempt}",
                        timeout=5,
                        retries=0,
                    )
                except Exception:
                    pass

            log.record(
                StepLog(
                    index=log.next_index(),
                    name=name,
                    status=StepStatus.RETRIED,
                    attempt=attempt,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    value=text,
                    error="captcha rejected by site",
                )
            )
        except Exception as e:
            log.record(
                StepLog(
                    index=log.next_index(),
                    name=name,
                    status=StepStatus.RETRIED,
                    attempt=attempt,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error=f"{type(e).__name__}: {e}",
                )
            )

    # Exhausted AI attempts.
    if source.lower() == "app":
        raise ScriptedAbort(
            f"CAPTCHA could not be solved after {max_ai_attempts} attempts"
        )

    # Human handoff path.
    started = time.monotonic()
    reason = (
        f"CAPTCHA could not be solved automatically after {max_ai_attempts} "
        f"attempts. Please type the captcha visible on screen and reply with "
        f"those characters."
    )
    human_text = await _wait_for_human_via_redis(job_id, r, reason)
    if not human_text:
        log.record(
            StepLog(
                index=log.next_index(),
                name=name,
                status=StepStatus.FAILED,
                duration_ms=int((time.monotonic() - started) * 1000),
                error="human_handoff_timeout",
                handoff_reason="captcha_human",
                handoff_cost_usd=total_cost,
            )
        )
        raise ScriptedAbort("CAPTCHA timed out waiting for human")

    await fill(
        session,
        input_selector,
        human_text.strip(),
        log=log,
        name=f"{name}.fill_human",
    )
    advanced = await submit_action()
    if advanced:
        log.record(
            StepLog(
                index=log.next_index(),
                name=name,
                status=StepStatus.HANDED_OFF,
                duration_ms=int((time.monotonic() - started) * 1000),
                value=human_text.strip(),
                handoff_reason="captcha_human",
                handoff_summary="Human typed captcha; form advanced.",
                handoff_cost_usd=total_cost,
            )
        )
        return

    log.record(
        StepLog(
            index=log.next_index(),
            name=name,
            status=StepStatus.FAILED,
            duration_ms=int((time.monotonic() - started) * 1000),
            value=human_text.strip(),
            error="human captcha also rejected",
            handoff_reason="captcha_human",
            handoff_cost_usd=total_cost,
        )
    )
    raise ScriptedAbort("CAPTCHA rejected even after human input")


# ─── image-captcha variant (securimage <img>, not a <canvas>) ────────────────
#
# Some portals (e.g. eCourts Virtual Courts securimage) render the captcha as an
# <img> whose src is a server endpoint that REGENERATES a new captcha on every
# GET — so we cannot re-fetch the URL to read it. Instead we screenshot the
# rendered <img> element via CDP (same technique as actions.save_qr_code) and
# OCR that PNG. Everything else (LLM OCR, refresh+retry, human/abort fallback)
# matches solve_canvas_captcha.


async def _read_img_png_b64(session, img_selector: str) -> str | None:
    """Screenshot the captcha <img> element and return its base64 PNG (no
    data-URI prefix), or None if the element is missing/zero-size.

    Waits for the <img> to fully load first (securimage paints after a beat);
    screenshotting a half-loaded image is the usual cause of an UNREADABLE OCR.
    """
    loaded_expr = (
        "(function(s){var e=document.querySelector(s);"
        "if(!(e instanceof HTMLImageElement)) return false;"
        "return !!(e.complete && e.naturalWidth > 0);"
        "})(" + json.dumps(img_selector) + ")"
    )
    _deadline = time.monotonic() + 8.0
    while time.monotonic() < _deadline:
        if await _cdp_eval(session, loaded_expr):
            break
        await asyncio.sleep(0.4)

    rect_expr = (
        "(function(s){var e=document.querySelector(s);"
        "if(!e) return null;"
        "var r=e.getBoundingClientRect();"
        "return {x:r.left,y:r.top,width:r.width,height:r.height};"
        "})(" + json.dumps(img_selector) + ")"
    )
    info = await _cdp_eval(session, rect_expr)
    if not info or info.get("width", 0) <= 0 or info.get("height", 0) <= 0:
        return None
    cdp = await session.get_or_create_cdp_session()
    shot = await cdp.cdp_client.send.Page.captureScreenshot(
        params={
            "format": "png",
            "clip": {
                "x": float(info["x"]),
                "y": float(info["y"]),
                "width": float(info["width"]),
                "height": float(info["height"]),
                "scale": 1,
            },
            "captureBeyondViewport": True,
        },
        session_id=cdp.session_id,
    )
    return shot.get("data")  # base64 PNG, no prefix — what _ocr_via_llm expects


async def solve_image_captcha(
    session,
    *,
    image_selector: str,
    input_selector: str,
    refresh_selector: str | None,
    submit_action: SubmitAction,
    job_id: str,
    r: redis.Redis,
    source: str,
    log: StepLogger,
    name: str = "solve_captcha",
    max_ai_attempts: int = 5,
) -> None:
    """Image-captcha counterpart of solve_canvas_captcha. Same contract, same
    fallback semantics (web → wait_for_human; app → ScriptedAbort). Raises
    ScriptedAbort if exhausted."""
    total_cost = 0.0

    for attempt in range(1, max_ai_attempts + 1):
        started = time.monotonic()
        try:
            await _wait_visible(session, image_selector, timeout=10)
            b64 = await _read_img_png_b64(session, image_selector)
            if not b64:
                raise RuntimeError("captcha image could not be screenshotted")

            text, cost = await _ocr_via_llm(b64)
            if not text or text.upper() == "UNREADABLE":
                # Fallback to the 1-step Agent OCR (same as solve_canvas_captcha).
                text2, cost2 = await _ocr_via_agent(session)
                text = text2
                cost += cost2
            total_cost += cost
            if not text or text.upper() == "UNREADABLE":
                raise RuntimeError("LLM could not read captcha")

            await fill(
                session,
                input_selector,
                text,
                log=log,
                name=f"{name}.fill_attempt_{attempt}",
            )

            advanced = await submit_action()
            if advanced:
                log.record(
                    StepLog(
                        index=log.next_index(),
                        name=name,
                        status=StepStatus.OK,
                        attempt=attempt,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        value=text,
                        handoff_reason="captcha_ocr",
                        handoff_cost_usd=total_cost,
                    )
                )
                return

            if refresh_selector and attempt < max_ai_attempts:
                try:
                    await click(
                        session,
                        refresh_selector,
                        log=log,
                        name=f"{name}.refresh_attempt_{attempt}",
                        timeout=5,
                        retries=0,
                    )
                except Exception:
                    pass

            log.record(
                StepLog(
                    index=log.next_index(),
                    name=name,
                    status=StepStatus.RETRIED,
                    attempt=attempt,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    value=text,
                    error="captcha rejected by site",
                )
            )
        except Exception as e:
            log.record(
                StepLog(
                    index=log.next_index(),
                    name=name,
                    status=StepStatus.RETRIED,
                    attempt=attempt,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error=f"{type(e).__name__}: {e}",
                )
            )

    # Exhausted AI attempts.
    if source.lower() == "app":
        raise ScriptedAbort(
            f"CAPTCHA could not be solved after {max_ai_attempts} attempts"
        )

    started = time.monotonic()
    reason = (
        f"CAPTCHA could not be solved automatically after {max_ai_attempts} "
        f"attempts. Please type the captcha visible on screen and reply with "
        f"those characters."
    )
    human_text = await _wait_for_human_via_redis(job_id, r, reason)
    if not human_text:
        log.record(
            StepLog(
                index=log.next_index(),
                name=name,
                status=StepStatus.FAILED,
                duration_ms=int((time.monotonic() - started) * 1000),
                error="human_handoff_timeout",
                handoff_reason="captcha_human",
                handoff_cost_usd=total_cost,
            )
        )
        raise ScriptedAbort("CAPTCHA timed out waiting for human")

    await fill(
        session,
        input_selector,
        human_text.strip(),
        log=log,
        name=f"{name}.fill_human",
    )
    advanced = await submit_action()
    if advanced:
        log.record(
            StepLog(
                index=log.next_index(),
                name=name,
                status=StepStatus.HANDED_OFF,
                duration_ms=int((time.monotonic() - started) * 1000),
                value=human_text.strip(),
                handoff_reason="captcha_human",
                handoff_summary="Human typed captcha; form advanced.",
                handoff_cost_usd=total_cost,
            )
        )
        return

    log.record(
        StepLog(
            index=log.next_index(),
            name=name,
            status=StepStatus.FAILED,
            duration_ms=int((time.monotonic() - started) * 1000),
            value=human_text.strip(),
            error="human captcha also rejected",
            handoff_reason="captcha_human",
            handoff_cost_usd=total_cost,
        )
    )
    raise ScriptedAbort("CAPTCHA rejected even after human input")
