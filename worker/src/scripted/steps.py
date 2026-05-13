# worker/src/scripted/steps.py
"""Step primitives.

Each primitive:
  - Records exactly one StepLog (with timing, attempt, status).
  - Raises a typed exception on irrecoverable failure (ScriptedAbort) or a
    generic exception that the caller can catch/translate.
  - Uses CDP Runtime.evaluate for the heavy lifting. The government portals
    we automate are Angular/jQuery soup; CDP eval is the most robust and
    matches the pattern already in worker/src/agent.py (save_qr_code,
    save_receipt).

All primitives are awaitable. The first positional arg is always the
BrowserSession (which IS the Browser instance -- they're aliases in
browser-use 0.12).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from .log import StepLogger
from .types import ScriptedAbort, StepLog, StepStatus


# ─── low-level CDP helpers ────────────────────────────────────────────────


async def _cdp_eval(
    session,
    expression: str,
    *,
    return_by_value: bool = True,
    await_promise: bool = True,
) -> Any:
    """Run JS in the page via CDP Runtime.evaluate. Returns the value (or
    None if the expression returned undefined / the call errored)."""
    cdp = await session.get_or_create_cdp_session()
    result = await cdp.cdp_client.send.Runtime.evaluate(
        params={
            "expression": expression,
            "returnByValue": return_by_value,
            "awaitPromise": await_promise,
        },
        session_id=cdp.session_id,
    )
    return (result.get("result", {}) or {}).get("value")


async def _current_url(session) -> str:
    return await _cdp_eval(session, "window.location.href") or ""


# ─── timing / logging helper ──────────────────────────────────────────────


def _log_ok(
    log: StepLogger,
    name: str,
    started: float,
    *,
    status: StepStatus = StepStatus.OK,
    selector: str | None = None,
    value: str | None = None,
    url: str | None = None,
    attempt: int = 1,
) -> None:
    log.record(
        StepLog(
            index=log.next_index(),
            name=name,
            status=status,
            selector=selector,
            value=value,
            url=url,
            attempt=attempt,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    )


def _log_fail(
    log: StepLogger,
    name: str,
    started: float,
    exc: BaseException,
    *,
    selector: str | None = None,
    value: str | None = None,
    url: str | None = None,
    attempt: int = 1,
) -> None:
    log.record(
        StepLog(
            index=log.next_index(),
            name=name,
            status=StepStatus.FAILED,
            selector=selector,
            value=value,
            url=url,
            attempt=attempt,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
    )


# ─── primitives ────────────────────────────────────────────────────────────


async def navigate(
    session,
    url: str,
    *,
    log: StepLogger,
    name: str,
    timeout: int = 30,
) -> None:
    """Navigate the current tab to url, then wait until document.readyState
    is 'complete' or the timeout fires."""
    started = time.monotonic()
    try:
        cdp = await session.get_or_create_cdp_session()
        await cdp.cdp_client.send.Page.navigate(
            params={"url": url}, session_id=cdp.session_id
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready = await _cdp_eval(session, "document.readyState")
            if ready == "complete":
                _log_ok(log, name, started, url=url)
                return
            await asyncio.sleep(0.25)
        raise TimeoutError(f"navigation to {url} did not complete in {timeout}s")
    except Exception as e:
        _log_fail(log, name, started, e, url=url)
        raise


async def _wait_visible(session, selector: str, *, timeout: int) -> None:
    """Internal: poll until the selector matches a visible element. No log."""
    deadline = time.monotonic() + timeout
    expr = (
        "(function(s){var e=document.querySelector(s);"
        "if(!e) return null;"
        "var r=e.getBoundingClientRect();"
        "return {w:r.width,h:r.height};"
        "})(" + json.dumps(selector) + ")"
    )
    while True:
        info = await _cdp_eval(session, expr)
        if info and info.get("w", 0) > 0 and info.get("h", 0) > 0:
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"selector not visible within {timeout}s: {selector}")
        await asyncio.sleep(0.25)


async def wait_for_selector(
    session,
    selector: str,
    *,
    log: StepLogger,
    name: str,
    timeout: int = 30,
) -> None:
    started = time.monotonic()
    try:
        await _wait_visible(session, selector, timeout=timeout)
        _log_ok(log, name, started, selector=selector)
    except Exception as e:
        _log_fail(log, name, started, e, selector=selector)
        raise


async def wait_for_url(
    session,
    contains: str,
    *,
    log: StepLogger,
    name: str,
    timeout: int = 30,
) -> None:
    """Wait until window.location.href contains the given substring."""
    started = time.monotonic()
    try:
        deadline = time.monotonic() + timeout
        last_url = ""
        while time.monotonic() < deadline:
            last_url = await _current_url(session)
            if contains in last_url:
                _log_ok(log, name, started, url=last_url)
                return
            await asyncio.sleep(0.5)
        raise TimeoutError(
            f"URL did not contain {contains!r} within {timeout}s; current={last_url}"
        )
    except Exception as e:
        _log_fail(log, name, started, e)
        raise


async def click(
    session,
    selector: str,
    *,
    log: StepLogger,
    name: str,
    timeout: int = 10,
    retries: int = 2,
) -> None:
    started = time.monotonic()
    last_err: Exception | None = None
    for attempt in range(1, retries + 2):
        try:
            await _wait_visible(session, selector, timeout=timeout)
            expr = (
                "(function(s){var e=document.querySelector(s);"
                "if(!e) return {ok:false,reason:'not_found'};"
                "e.click(); return {ok:true};"
                "})(" + json.dumps(selector) + ")"
            )
            res = await _cdp_eval(session, expr)
            if not res or not res.get("ok"):
                raise RuntimeError((res or {}).get("reason") or "click_failed")
            _log_ok(
                log,
                name,
                started,
                selector=selector,
                attempt=attempt,
                status=StepStatus.OK if attempt == 1 else StepStatus.RETRIED,
            )
            return
        except Exception as e:
            last_err = e
            await asyncio.sleep(0.5 * attempt)
    _log_fail(
        log,
        name,
        started,
        last_err or RuntimeError("click failed"),
        selector=selector,
        attempt=retries + 1,
    )
    raise last_err or RuntimeError("click failed")


async def click_by_text(
    session,
    text: str,
    *,
    log: StepLogger,
    name: str,
    tag: str = "button",
    timeout: int = 10,
) -> None:
    """Click the first visible <tag> whose textContent equals (or contains) text."""
    started = time.monotonic()
    try:
        deadline = time.monotonic() + timeout
        expr = (
            "(function(tag,txt){"
            "var els=document.querySelectorAll(tag);"
            "var trimmed=(txt||'').trim().toUpperCase();"
            "var exact=null, partial=null;"
            "for(var i=0;i<els.length;i++){"
            "  var el=els[i]; var t=(el.textContent||'').trim();"
            "  var r=el.getBoundingClientRect();"
            "  if(r.width<=0||r.height<=0) continue;"
            "  var up=t.toUpperCase();"
            "  if(up===trimmed){exact=el;break;}"
            "  if(!partial && up.indexOf(trimmed)>=0){partial=el;}"
            "}"
            "var hit=exact||partial;"
            "if(hit){hit.click(); return {ok:true};} else {return {ok:false};}"
            "})(" + json.dumps(tag) + "," + json.dumps(text) + ")"
        )
        while True:
            res = await _cdp_eval(session, expr)
            if res and res.get("ok"):
                _log_ok(log, name, started, value=text)
                return
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"no clickable {tag} with text within {timeout}s: {text!r}"
                )
            await asyncio.sleep(0.25)
    except Exception as e:
        _log_fail(log, name, started, e, value=text)
        raise


async def fill(
    session,
    selector: str,
    value: str,
    *,
    log: StepLogger,
    name: str,
    timeout: int = 10,
) -> None:
    """Set the value of an <input>/<textarea> via the native value setter,
    then dispatch input + change so Angular/React see it."""
    started = time.monotonic()
    try:
        await _wait_visible(session, selector, timeout=timeout)
        expr = (
            "(function(s,v){var e=document.querySelector(s);"
            "if(!e) return {ok:false};"
            "e.focus();"
            "var proto = e instanceof HTMLTextAreaElement"
            "  ? window.HTMLTextAreaElement.prototype"
            "  : window.HTMLInputElement.prototype;"
            "var d = Object.getOwnPropertyDescriptor(proto,'value');"
            "if(d && d.set){ d.set.call(e, v); } else { e.value = v; }"
            "e.dispatchEvent(new Event('input',{bubbles:true}));"
            "e.dispatchEvent(new Event('change',{bubbles:true}));"
            "return {ok:true};"
            "})(" + json.dumps(selector) + "," + json.dumps(value) + ")"
        )
        res = await _cdp_eval(session, expr)
        if not res or not res.get("ok"):
            raise RuntimeError("fill failed (element not found or not fillable)")
        masked = value if len(value) < 80 else value[:40] + "..." + value[-10:]
        _log_ok(log, name, started, selector=selector, value=masked)
    except Exception as e:
        _log_fail(log, name, started, e, selector=selector, value=value[:80])
        raise


async def select_by_text(
    session,
    selector: str,
    text: str,
    *,
    log: StepLogger,
    name: str,
    timeout: int = 30,
) -> None:
    """Set a native <select>'s value to the option whose visible text matches.

    Match order: exact -> case-insensitive equal -> case-insensitive contains.

    Polls every 0.5s for up to `timeout` seconds. Before each option read,
    dispatches focus + mousedown + mouseup on every matching element so
    Angular's lazy bindings activate. Iterates ALL elements returned by
    querySelectorAll(selector) -- the first <select> that has a matching
    option wins.
    """
    started = time.monotonic()
    try:
        await _wait_visible(session, selector, timeout=timeout)
        deadline = time.monotonic() + timeout

        # NOTE: the wakeup block at the top of the expression triggers any
        # Angular bindings that lazy-load options. Then we proceed with the
        # same match-and-dump logic from v2.
        expr = (
            "(function(s,t){"
            "var els=document.querySelectorAll(s);"
            "for(var w=0;w<els.length;w++){"
            "  var we=els[w];"
            "  if(!(we instanceof HTMLSelectElement)){continue;}"
            "  try{"
            "    we.focus();"
            "    we.dispatchEvent(new FocusEvent('focusin',{bubbles:true}));"
            "    we.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));"
            "    we.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,cancelable:true}));"
            "  }catch(err){}"
            "}"
            "var trimmed=(t||'').trim();"
            "var upper=trimmed.toUpperCase();"
            "var dump=[];"
            "for(var ei=0;ei<els.length;ei++){"
            "  var e=els[ei];"
            "  if(!(e instanceof HTMLSelectElement)){continue;}"
            "  var r=e.getBoundingClientRect();"
            "  var visible=(r.width>0 && r.height>0);"
            "  var texts=[],values=[];"
            "  for(var oi=0;oi<e.options.length;oi++){"
            "    texts.push((e.options[oi].text||'').trim());"
            "    values.push(e.options[oi].value);"
            "  }"
            "  dump.push({idx:ei,optionCount:e.options.length,"
            "             optionTexts:texts,optionValues:values,"
            "             visible:visible,classes:e.className||''});"
            "  var match=null;"
            "  for(var i=0;i<e.options.length;i++){"
            "    if((e.options[i].text||'').trim()===trimmed){match=e.options[i];break;}}"
            "  if(!match){for(var j=0;j<e.options.length;j++){"
            "    if((e.options[j].text||'').trim().toUpperCase()===upper){"
            "      match=e.options[j];break;}}}"
            "  if(!match){for(var k=0;k<e.options.length;k++){"
            "    var u=(e.options[k].text||'').trim().toUpperCase();"
            "    if(u && u.indexOf(upper)>=0){match=e.options[k];break;}}}"
            "  if(match){"
            "    e.value=match.value;"
            "    e.dispatchEvent(new Event('input',{bubbles:true}));"
            "    e.dispatchEvent(new Event('change',{bubbles:true}));"
            "    return {ok:true,elementIndex:ei,value:match.value,"
            "            text:(match.text||'').trim()};"
            "  }"
            "}"
            "return {ok:false,reason:'option_not_found',elements:dump};"
            "})(" + json.dumps(selector) + "," + json.dumps(text) + ")"
        )

        last_dump = None
        while True:
            res = await _cdp_eval(session, expr)
            if res and res.get("ok"):
                _log_ok(log, name, started, selector=selector, value=text)
                return
            if res:
                last_dump = res.get("elements")
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"option_not_found after {timeout}s polling; "
                    f"elements_seen={last_dump}"
                )
            await asyncio.sleep(0.5)
    except Exception as e:
        _log_fail(log, name, started, e, selector=selector, value=text)
        raise


async def select_by_value(
    session,
    selector: str,
    value: str,
    *,
    log: StepLogger,
    name: str,
    timeout: int = 30,
) -> None:
    """Set a native <select>'s value directly.

    Polls every 0.5s for up to `timeout` seconds. Before each read,
    dispatches focus + mousedown + mouseup events on every matching
    element to trigger Angular's lazy bindings. Iterates ALL elements
    returned by querySelectorAll(selector) -- the first <select> that has
    the requested value wins.
    """
    started = time.monotonic()
    try:
        await _wait_visible(session, selector, timeout=timeout)
        deadline = time.monotonic() + timeout

        expr = (
            "(function(s,v){"
            "var els=document.querySelectorAll(s);"
            "for(var w=0;w<els.length;w++){"
            "  var we=els[w];"
            "  if(!(we instanceof HTMLSelectElement)){continue;}"
            "  try{"
            "    we.focus();"
            "    we.dispatchEvent(new FocusEvent('focusin',{bubbles:true}));"
            "    we.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));"
            "    we.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,cancelable:true}));"
            "  }catch(err){}"
            "}"
            "var dump=[];"
            "for(var ei=0;ei<els.length;ei++){"
            "  var e=els[ei];"
            "  if(!(e instanceof HTMLSelectElement)){continue;}"
            "  var r=e.getBoundingClientRect();"
            "  var visible=(r.width>0 && r.height>0);"
            "  var texts=[],values=[];"
            "  for(var oi=0;oi<e.options.length;oi++){"
            "    texts.push((e.options[oi].text||'').trim());"
            "    values.push(e.options[oi].value);"
            "  }"
            "  dump.push({idx:ei,optionCount:e.options.length,"
            "             optionTexts:texts,optionValues:values,"
            "             visible:visible,classes:e.className||''});"
            "  var found=false;"
            "  for(var i=0;i<e.options.length;i++){"
            "    if(e.options[i].value===v){found=true;break;}}"
            "  if(found){"
            "    e.value=v;"
            "    e.dispatchEvent(new Event('input',{bubbles:true}));"
            "    e.dispatchEvent(new Event('change',{bubbles:true}));"
            "    return {ok:true,elementIndex:ei};"
            "  }"
            "}"
            "return {ok:false,reason:'value_not_found',elements:dump};"
            "})(" + json.dumps(selector) + "," + json.dumps(value) + ")"
        )

        last_dump = None
        while True:
            res = await _cdp_eval(session, expr)
            if res and res.get("ok"):
                _log_ok(log, name, started, selector=selector, value=value)
                return
            if res:
                last_dump = res.get("elements")
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"value_not_found after {timeout}s polling; "
                    f"elements_seen={last_dump}"
                )
            await asyncio.sleep(0.5)
    except Exception as e:
        _log_fail(log, name, started, e, selector=selector, value=value)
        raise


async def abort_if_popup_text(
    session,
    keywords: list[str],
    abort_reason: str,
    *,
    log: StepLogger,
    name: str,
    close_selector: str | None = None,
) -> None:
    """Inspect any visible modal/popup; if its text contains any keyword
    (case-insensitive), optionally click close_selector then raise
    ScriptedAbort(abort_reason). Otherwise log OK and return."""
    started = time.monotonic()
    try:
        expr = (
            "(function(kw){"
            "var sel='.modal,.modal-content,.popup,[role=\"dialog\"],.swal2-popup';"
            "var nodes=document.querySelectorAll(sel);"
            "var text='';"
            "for(var i=0;i<nodes.length;i++){"
            "  var n=nodes[i]; var r=n.getBoundingClientRect();"
            "  if(r.width>0 && r.height>0){ text += (n.textContent||'') + ' '; }"
            "}"
            "if(!text) return {found:false};"
            "var up=text.toUpperCase();"
            "for(var j=0;j<kw.length;j++){"
            "  if(up.indexOf(kw[j].toUpperCase())>=0) return {found:true,matched:kw[j]};"
            "}"
            "return {found:false};"
            "})(" + json.dumps(keywords) + ")"
        )
        res = await _cdp_eval(session, expr)
        if res and res.get("found"):
            matched = res.get("matched")
            if close_selector:
                try:
                    await _cdp_eval(
                        session,
                        "var e=document.querySelector("
                        + json.dumps(close_selector)
                        + "); if(e) e.click();",
                    )
                except Exception:
                    pass
            _log_fail(
                log,
                name,
                started,
                ScriptedAbort(abort_reason),
                value=f"matched={matched}",
            )
            raise ScriptedAbort(abort_reason)
        _log_ok(log, name, started)
    except ScriptedAbort:
        raise
    except Exception as e:
        _log_fail(log, name, started, e)
        raise


async def get_text_by_selector(session, selector: str) -> str:
    """Trimmed textContent of the first match. '' if not found. No log entry --
    used inside higher-level steps like receipt extraction."""
    expr = (
        "(function(s){var e=document.querySelector(s);"
        "return e ? (e.textContent||'').trim() : '';"
        "})(" + json.dumps(selector) + ")"
    )
    return await _cdp_eval(session, expr) or ""


async def sleep_seconds(
    secs: float,
    *,
    log: StepLogger,
    name: str = "sleep",
) -> None:
    started = time.monotonic()
    await asyncio.sleep(secs)
    _log_ok(log, name, started, value=f"{secs}s")


async def get_select_value(session, selector: str) -> str | None:
    """Returns the currently selected value of a <select>:
        - non-empty string if a real option is selected
        - "" if the placeholder/empty option is selected
        - None if the element isn't a <select> (or doesn't exist)
    No log entry -- intended for use inside higher-level step compositions.
    """
    expr = (
        "(function(s){var e=document.querySelector(s);"
        "if(!(e instanceof HTMLSelectElement)) return null;"
        "return e.value;"
        "})(" + json.dumps(selector) + ")"
    )
    return await _cdp_eval(session, expr)
