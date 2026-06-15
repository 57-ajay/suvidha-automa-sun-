"""Pure-function implementations of the actions that today live as tool
closures inside agent.py's make_tools().

Why this split:
  - The AI path (agent.py) needs them as @tools.action decorated closures so
    browser-use can register them on the Agent.
  - The scripted path (scripted/) needs them as plain async functions it can
    call directly.

After this refactor, agent.py's @tools.action wrappers become one-liners
that call into here. The actual logic (CDP eval for the QR, Page.printToPDF
for the receipt, Redis flag for wait_for_human) lives here once and is
shared by both paths.

The signatures intentionally accept the BrowserSession / Redis client / job
context as plain arguments rather than relying on browser-use's tool
parameter injection.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any

import httpx
import redis

from browser_use import BrowserSession


API_URL = os.environ.get("API_URL", "http://api:3000")
HUMAN_WAIT_TIMEOUT = 200
JOB_TTL = 60 * 60 * 24


# ─── wait_for_human ────────────────────────────────────────────────────────


async def wait_for_human(
    job_id: str,
    r: redis.Redis,
    reason: str,
    *,
    timeout: int = HUMAN_WAIT_TIMEOUT,
) -> str:
    """Set job status to waiting_for_human, poll Redis for humanInput, return
    the human's reply (or '' with a 'TIMEOUT: ...' style sentinel if no reply
    arrived within `timeout` seconds).

    Behavior is identical to the existing agent.py tool of the same name --
    the tool now just calls this."""
    print(f"[{job_id}] Waiting for human (timeout={timeout}s): {reason}")

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
            print(f"[{job_id}] Human done: {text}")
            return text
        await asyncio.sleep(1)
        waited += 1

    # Timeout path: record reason, return back to running, return sentinel.
    r.hdel(f"job:{job_id}", "waitReason")
    r.hset(f"job:{job_id}", "status", "running")
    r.rpush(f"job:{job_id}:partial_reasons", f"human_timeout:{reason}")
    r.expire(f"job:{job_id}:partial_reasons", JOB_TTL)
    print(f"[{job_id}] Human timeout after {timeout}s: {reason}")
    return (
        f"TIMEOUT: No human response after {timeout} seconds. "
        f"Reason was: {reason}. Do NOT call wait_for_human again. "
        "Save any partial data via save_challans / save_discounts / "
        "save_receipt, then complete with 'Status: partial'."
    )


# ─── save_qr_code ──────────────────────────────────────────────────────────
#
# Per-state QR locators
# =====================
# Each constant below is a self-contained CDP-evaluated JS expression that
# returns either:
#   - { src, isDataUri, x, y, width, height }  ← QR found
#   - null                                      ← no QR found
#
# Adding a new state? Add a `_QR_LOCATE_<STATE>` constant here, then register
# it in `_QR_LOCATORS_BY_STATE` below. The return-shape contract is the only
# thing each locator MUST honor; everything else (decode, screenshot fallback,
# upload) is shared logic in save_qr_code() further down.
#
# When the portal changes selectors? Edit ONLY the matching constant. The
# other states' extractors are untouched.

# UP / HR / PB — SBIePay Lite
# All three use the same SBIePay Lite payment gateway and the QR <img> always
# has id="qrcodeImg". One constant covers them.
_QR_LOCATE_UP_HR_PB = """
(function() {
    var img = document.getElementById('qrcodeImg');
    if (!img) return null;
    var src  = img.src || '';
    var rect = img.getBoundingClientRect();
    return {
        src: src,
        isDataUri: src.startsWith('data:'),
        x: rect.left, y: rect.top,
        width: rect.width, height: rect.height
    };
})()
"""

# RJ — eGRAS Rajasthan (ASP.NET WebForms)
# The QR <img> has no id but lives inside a container div with an
# ASP.NET-prefixed id. Older actions.py had a 'ct100' typo fallback; we keep
# both spellings because some early jobs still reference the typoed one.
_QR_LOCATE_RJ = """
(function() {
    var container = document.getElementById('ctl00_ContentPlaceHolder1_divQRCode')
        || document.getElementById('ct100_ContentPlaceHolder1_divQRCode');
    if (!container) return null;
    var img = container.querySelector('img');
    if (!img) return null;
    var src  = img.src || '';
    var rect = img.getBoundingClientRect();
    return {
        src: src,
        isDataUri: src.startsWith('data:'),
        x: rect.left, y: rect.top,
        width: rect.width, height: rect.height
    };
})()
"""

# MP — SBIePay (NOT SBIePay Lite) at epay.sbi.bank.in/secure/upiQRWait.jsp
# The QR <img> has NO id and sits inside a generic <div>. We identify it by:
#   - data:image/png;base64 src (the QR is embedded inline, not a remote URL)
#   - >= 200x200 px (filters out logos/icons)
#   - aspect ratio 0.85-1.15 (QR codes are square)
#   - LARGEST area among matches (defeats accidental small-image matches)
# This pattern is also a sensible fallback for any future state that embeds
# an inline data-URI QR with no convenient selector.
_QR_LOCATE_MP = """
(function() {
    var candidates = document.querySelectorAll("img[src^='data:image/png;base64']");
    var best = null;
    var bestArea = 0;
    for (var i = 0; i < candidates.length; i++) {
        var c = candidates[i];
        var r = c.getBoundingClientRect();
        if (r.width < 200 || r.height < 200) continue;
        var aspect = r.width / r.height;
        if (aspect < 0.85 || aspect > 1.15) continue;
        var area = r.width * r.height;
        if (area > bestArea) {
            best = c;
            bestArea = area;
        }
    }
    if (!best) return null;
    var src  = best.src || '';
    var rect = best.getBoundingClientRect();
    return {
        src: src,
        isDataUri: src.startsWith('data:'),
        x: rect.left, y: rect.top,
        width: rect.width, height: rect.height
    };
})()
"""

# GENERIC — used when:
#   1. The AI agent path calls save_qr_code (params may not include `state`).
#   2. A new state hits save_qr_code before its per-state locator is added.
# This is a defensive cascade of all known patterns above. Order matters:
# try precise selectors first, generic data-URI search last. If you find
# yourself relying on this in a NEW scripted state, add a dedicated locator
# above and register it in _QR_LOCATORS_BY_STATE instead.
_QR_LOCATE_GENERIC = """
(function() {
    // 1. SBIePay Lite (UP/HR/PB)
    var img = document.getElementById('qrcodeImg');

    // 2. eGRAS Rajasthan (RJ)
    if (!img) {
        var container = document.getElementById('ctl00_ContentPlaceHolder1_divQRCode')
            || document.getElementById('ct100_ContentPlaceHolder1_divQRCode');
        if (container) img = container.querySelector('img');
    }

    // 3. Generic large-square data-URI fallback (MP and any future
    //    state that embeds a base64 QR with no convenient selector).
    if (!img) {
        var candidates = document.querySelectorAll("img[src^='data:image/png;base64']");
        var best = null;
        var bestArea = 0;
        for (var i = 0; i < candidates.length; i++) {
            var c = candidates[i];
            var r = c.getBoundingClientRect();
            if (r.width < 200 || r.height < 200) continue;
            var aspect = r.width / r.height;
            if (aspect < 0.85 || aspect > 1.15) continue;
            var area = r.width * r.height;
            if (area > bestArea) { best = c; bestArea = area; }
        }
        if (best) img = best;
    }

    if (!img) return null;
    var src  = img.src || '';
    var rect = img.getBoundingClientRect();
    return {
        src: src,
        isDataUri: src.startsWith('data:'),
        x: rect.left, y: rect.top,
        width: rect.width, height: rect.height
    };
})()
"""

# Per-state dispatch table. Keys are upper-cased state codes / names.
# When job_params['state'] matches a key, the corresponding JS runs ALONE.
# When it doesn't, _QR_LOCATE_GENERIC runs (the cascade) so the AI path and
# any unregistered state still works.
_QR_LOCATORS_BY_STATE: dict[str, str] = {
    # SBIePay Lite trio
    "UP": _QR_LOCATE_UP_HR_PB,
    "U.P.": _QR_LOCATE_UP_HR_PB,
    "UTTAR PRADESH": _QR_LOCATE_UP_HR_PB,
    "HR": _QR_LOCATE_UP_HR_PB,
    "HARYANA": _QR_LOCATE_UP_HR_PB,
    "PB": _QR_LOCATE_UP_HR_PB,
    "PUNJAB": _QR_LOCATE_UP_HR_PB,
    # eGRAS RJ (ASP.NET WebForms)
    "RJ": _QR_LOCATE_RJ,
    "RAJASTHAN": _QR_LOCATE_RJ,
    # SBIePay (epay.sbi.bank.in)
    "MP": _QR_LOCATE_MP,
    "M.P.": _QR_LOCATE_MP,
    "MADHYA PRADESH": _QR_LOCATE_MP,
}


def _pick_qr_locator(job_params: dict) -> tuple[str, str]:
    """Return (locator_name, js_expression) based on job_params['state'].

    Falls back to the generic cascade if state is missing or unrecognized.
    The locator_name is logged so it's obvious from the job log which
    extractor ran -- useful when a portal changes its selectors.
    """
    state = (job_params.get("state") or "").strip().upper()
    js = _QR_LOCATORS_BY_STATE.get(state)
    if js is not None:
        return (f"state={state}", js)
    return ("generic_cascade", _QR_LOCATE_GENERIC)


async def save_qr_code(
    session: BrowserSession,
    job_id: str,
    job_params: dict,
) -> dict:
    """Capture the visible UPI QR-code image and POST it to
    /api/internal/border-tax/save-qr. Returns the parsed API response.

    QR location is picked per-state from `_QR_LOCATORS_BY_STATE` using
    `job_params['state']`; falls back to a generic cascade for the AI
    agent path or unregistered states.
    """
    locator_name, locate_js = _pick_qr_locator(job_params)
    print(f"[{job_id}] save_qr_code called (locator: {locator_name})")

    try:
        cdp = await session.get_or_create_cdp_session()

        # 1. Locate the QR <img> via CDP eval.
        eval_result = await cdp.cdp_client.send.Runtime.evaluate(
            params={"expression": locate_js, "returnByValue": True},
            session_id=cdp.session_id,
        )
        info = (eval_result.get("result", {}) or {}).get("value")
        if not info:
            msg = (
                f"QR code img element not found by locator '{locator_name}'. "
                f"If this is a new state portal, add a _QR_LOCATE_<STATE> "
                f"entry in actions.py and register it in _QR_LOCATORS_BY_STATE."
            )
            print(f"[{job_id}]   ERROR: {msg}")
            return {"ok": False, "error": msg}

        if info.get("width", 0) <= 0 or info.get("height", 0) <= 0:
            msg = (
                f"QR element found by '{locator_name}' but has zero size "
                f"(w={info.get('width')} h={info.get('height')})"
            )
            print(f"[{job_id}]   ERROR: {msg}")
            return {"ok": False, "error": msg}

        # 2. Get the PNG bytes -- either decode the data: URI or screenshot
        # the element via CDP Page.captureScreenshot with a clip rect.
        img_bytes: bytes | None = None
        if info.get("isDataUri"):
            try:
                _, b64 = info["src"].split(",", 1)
                img_bytes = base64.b64decode(b64)
            except Exception as e:
                print(f"[{job_id}]   data-URI decode failed: {e}")
                img_bytes = None

        if not img_bytes:
            clip = {
                "x": float(info["x"]),
                "y": float(info["y"]),
                "width": float(info["width"]),
                "height": float(info["height"]),
                "scale": 1,
            }
            shot = await cdp.cdp_client.send.Page.captureScreenshot(
                params={
                    "format": "png",
                    "clip": clip,
                    "captureBeyondViewport": True,
                },
                session_id=cdp.session_id,
            )
            data_b64 = shot.get("data")
            if not data_b64:
                return {
                    "ok": False,
                    "error": "Page.captureScreenshot returned no data",
                }
            img_bytes = base64.b64decode(data_b64)

        print(
            f"[{job_id}]   QR PNG ready ({len(img_bytes)} bytes); "
            f"uploading to {API_URL}/api/internal/border-tax/save-qr"
        )

        # 3. Upload to the API.
        files = {"image": ("qr_code.png", img_bytes, "image/png")}
        form_data = {
            "jobId": job_id,
            "params": json.dumps(job_params),
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{API_URL}/api/internal/border-tax/save-qr",
                files=files,
                data=form_data,
            )

        print(f"[{job_id}] save_qr_code response: {resp.status_code}")
        try:
            return resp.json()
        except Exception:
            return {"ok": False, "error": f"non-JSON response: {resp.text[:300]}"}

    except Exception as e:
        msg = f"save_qr_code error: {type(e).__name__}: {e}"
        print(f"[{job_id}]   ERROR: {msg}")
        return {"ok": False, "error": msg}


# ─── save_receipt ──────────────────────────────────────────────────────────


async def save_receipt(
    session,
    job_id: str,
    job_params: dict,
    data: Any,
    endpoint: str = "/api/internal/border-tax/save-receipt",
) -> dict:
    """Capture the currently-visible receipt page as a PDF via CDP
    Page.printToPDF, then upload it + the receipt metadata to
    /api/internal/border-tax/save-receipt as multipart/form-data.

    `data` accepts dict, single-element list of one dict, or a JSON string."""
    print(f"[{job_id}] save_receipt called")

    # ─── 1. Normalize data ────────────────────────────────────────────
    if isinstance(data, str):
        try:
            data = json.loads(data.strip())
        except json.JSONDecodeError as e:
            msg = f"data is a string but not valid JSON: {e}"
            print(f"[{job_id}]   ERROR: {msg}")
            return {"ok": False, "error": msg}

    if isinstance(data, list):
        if len(data) == 1 and isinstance(data[0], dict):
            data = data[0]
        else:
            msg = f"data must be a single object, got list of length {len(data)}"
            print(f"[{job_id}]   ERROR: {msg}")
            return {"ok": False, "error": msg}

    if not isinstance(data, dict):
        msg = f"data must be an object, got {type(data).__name__}"
        print(f"[{job_id}]   ERROR: {msg}")
        return {"ok": False, "error": msg}

    # ─── 2. Render the page to PDF via CDP ────────────────────────────
    try:
        cdp = await session.get_or_create_cdp_session()
        pdf_resp = await cdp.cdp_client.send.Page.printToPDF(
            params={
                "landscape": False,
                "printBackground": True,
                "preferCSSPageSize": True,
            },
            session_id=cdp.session_id,
        )
        pdf_b64 = pdf_resp.get("data")
        if not pdf_b64:
            return {"ok": False, "error": "Page.printToPDF returned no data"}
        pdf_bytes = base64.b64decode(pdf_b64)
    except Exception as e:
        msg = f"Page.printToPDF failed: {type(e).__name__}: {e}"
        print(f"[{job_id}]   ERROR: {msg}")
        return {"ok": False, "error": msg}

    if len(pdf_bytes) < 1000:
        # Server-side will also validate; we just warn here.
        print(f"[{job_id}]   WARNING: PDF suspiciously small ({len(pdf_bytes)} bytes)")

    # ─── 3. Upload ────────────────────────────────────────────────────
    receipt_no = str(data.get("receiptNumber", "receipt"))
    safe_name = "".join(c for c in receipt_no if c.isalnum() or c in "-_")
    filename = f"{safe_name}_receipt.pdf" if safe_name else "receipt.pdf"

    files = {"pdf": (filename, pdf_bytes, "application/pdf")}
    form_data = {
        "jobId": job_id,
        "params": json.dumps(job_params),
        "data": json.dumps(data),
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{API_URL}{endpoint}",
                files=files,
                data=form_data,
            )
        print(f"[{job_id}] save_receipt response: {resp.status_code}")
        try:
            return resp.json()
        except Exception:
            return {"ok": False, "error": f"non-JSON response: {resp.text[:300]}"}
    except Exception as e:
        msg = f"save_receipt HTTP error: {type(e).__name__}: {e}"
        print(f"[{job_id}]   ERROR: {msg}")
        return {"ok": False, "error": msg}
