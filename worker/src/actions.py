# worker/src/actions.py
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


_QR_LOCATE_JS = """
(function() {
    // UP/HR: <img id="qrcodeImg">
    var img = document.getElementById('qrcodeImg');

    // RJ: <img> inside <div id="ct100_ContentPlaceHolder1_divQRCode">
    if (!img) {
        var container = document.getElementById('ctl00_ContentPlaceHolder1_divQRCode')
        || document.getElementById('ct100_ContentPlaceHolder1_divQRCode');
        if (container) img = container.querySelector('img');
    }

    if (!img) return null;

    var src  = img.src || '';
    var rect = img.getBoundingClientRect();
    return {
        src:       src,
        isDataUri: src.startsWith('data:'),
        x:         rect.left,
        y:         rect.top,
        width:     rect.width,
        height:    rect.height
    };
})()
"""


async def save_qr_code(
    session: BrowserSession,
    job_id: str,
    job_params: dict,
) -> dict:
    """Capture the visible UPI QR-code image and POST it to
    /api/internal/border-tax/save-qr. Returns the parsed API response."""
    print(f"[{job_id}] save_qr_code called")

    try:
        cdp = await session.get_or_create_cdp_session()

        # 1. Locate the QR <img> via CDP eval.
        eval_result = await cdp.cdp_client.send.Runtime.evaluate(
            params={"expression": _QR_LOCATE_JS, "returnByValue": True},
            session_id=cdp.session_id,
        )
        info = (eval_result.get("result", {}) or {}).get("value")
        if not info:
            msg = (
                "QR code img element not found -- tried #qrcodeImg (UP/HR) "
                "and #ct100_ContentPlaceHolder1_divQRCode img (RJ)"
            )
            print(f"[{job_id}]   ERROR: {msg}")
            return {"ok": False, "error": msg}

        if info.get("width", 0) <= 0 or info.get("height", 0) <= 0:
            msg = (
                f"QR element found but has zero size "
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
                f"{API_URL}/api/internal/border-tax/save-receipt",
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
