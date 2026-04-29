import asyncio
import base64
import json
import os

import httpx
import redis
from browser_use import Agent, Browser, ChatGoogle, Tools, BrowserSession

API_URL = os.environ.get("API_URL", "http://api:3000")
HUMAN_WAIT_TIMEOUT = 200
JOB_TTL = 60 * 60 * 24


def make_tools(
    job_id: str,
    job_params: dict,
    tool_defs: list,
    r: redis.Redis,
    task_id: str = "",
) -> Tools:
    tools = Tools()

    # -- always available: wait_for_human --
    @tools.action(
        description=(
            "Call this when you need human help. "
            "Pass a reason (e.g. 'OTP required', 'CAPTCHA needs solving'). "
            "The human can interact with the browser directly via the live view, "
            "or send a text response via API. "
            "Returns the human's response when they are finished.\n\n"
            f"IMPORTANT — TIMEOUT BEHAVIOR: If no human response arrives within "
            f"{HUMAN_WAIT_TIMEOUT} seconds, this tool returns a string starting with "
            "'TIMEOUT:'. When you see TIMEOUT:\n"
            "  1. Do NOT call wait_for_human again — the human is not available.\n"
            "  2. Save any partial data you have already extracted "
            "(save_challans / save_discounts / save_receipt as applicable).\n"
            "  3. Finish with 'Status: partial' in your final summary."
        )
    )
    async def wait_for_human(reason: str) -> str:
        print(f"[{job_id}] Waiting for human (timeout={
              HUMAN_WAIT_TIMEOUT}s): {reason}")

        r.hset(f"job:{job_id}", mapping={
            "status": "waiting_for_human",
            "waitReason": reason,
        })

        waited = 0
        while waited < HUMAN_WAIT_TIMEOUT:
            human_input = r.hget(f"job:{job_id}", "humanInput")
            if human_input:
                human_input = human_input.decode() if isinstance(
                    human_input, bytes) else human_input
                r.hdel(f"job:{job_id}", "humanInput", "waitReason")
                r.hset(f"job:{job_id}", "status", "running")
                print(f"[{job_id}] Human done: {human_input}")
                return human_input
            await asyncio.sleep(1)
            waited += 1

        # Timeout — record partial reason, flip status back to running, let the agent continue
        r.hdel(f"job:{job_id}", "waitReason")
        r.hset(f"job:{job_id}", "status", "running")
        r.rpush(f"job:{job_id}:partial_reasons", f"human_timeout:{reason}")
        r.expire(f"job:{job_id}:partial_reasons", JOB_TTL)
        print(f"[{job_id}] Human timeout after {
              HUMAN_WAIT_TIMEOUT}s: {reason}")
        return (
            f"TIMEOUT: No human response after {HUMAN_WAIT_TIMEOUT} seconds. "
            f"Reason was: {reason}. Do NOT call wait_for_human again. "
            "Save any partial data via save_challans / save_discounts / save_receipt, "
            "then complete with 'Status: partial'."
        )

    # -- border-tax-only: save_receipt (captures the rendered receipt page as PDF) --
    if task_id == "border-tax":
        @tools.action(
            description=(
                "Capture the currently visible receipt page as a PDF, upload "
                "it to cloud storage, and persist the receipt metadata. Call "
                "this EXACTLY ONCE after verifying the receipt page is fully "
                "rendered. Do NOT click the Print button — this tool captures "
                "the page directly via the browser's native print engine, no "
                "system dialog is involved.\n\n"
                "Pass an object as `data` with these fields:\n"
                "  - vehicleNumber (string)\n"
                "  - receiptNumber (string)\n"
                "  - amount (number, in Rs, no currency symbol)\n"
                "  - paymentDate (string, YYYY-MM-DD)\n\n"
                "Example data: "
                "{\"vehicleNumber\":\"HR55AZ1101\","
                "\"receiptNumber\":\"UPR2604280468752\","
                "\"amount\":120,"
                "\"paymentDate\":\"2026-04-28\"}\n\n"
                "Returns JSON. Confirm both \"ok\": true AND "
                "\"pdfUploaded\": true to consider the call fully successful. "
                "If \"ok\": false, do NOT retry — record the error and complete "
                "with 'Status: partial'."
            )
        )
        async def save_receipt(data, browser_session: BrowserSession) -> str:
            print(f"[{job_id}] save_receipt called")
            print(f"[{job_id}]   raw data type: {type(data).__name__}")
            print(f"[{job_id}]   raw data preview: {str(data)[:300]}")

            # ─── 1. Normalize data: accept dict, JSON string, or single-element list ───
            if isinstance(data, str):
                try:
                    data = json.loads(data.strip())
                except json.JSONDecodeError as e:
                    msg = f"data is a string but not valid JSON: {e}"
                    print(f"[{job_id}]   ERROR: {msg}")
                    return json.dumps({"ok": False, "error": msg})

            if isinstance(data, list):
                if len(data) == 1 and isinstance(data[0], dict):
                    data = data[0]
                else:
                    msg = f"data must be a single object, got list of length {
                        len(data)}"
                    print(f"[{job_id}]   ERROR: {msg}")
                    return json.dumps({"ok": False, "error": msg})

            if not isinstance(data, dict):
                msg = f"data must be an object, got {type(data).__name__}"
                print(f"[{job_id}]   ERROR: {msg}")
                return json.dumps({"ok": False, "error": msg})

            # ─── 2. Validate required fields ───
            required = ["vehicleNumber", "receiptNumber",
                        "amount", "paymentDate"]
            missing = [
                f for f in required
                if f not in data or data[f] in ("", None)
            ]
            if missing:
                msg = f"Missing required fields: {', '.join(missing)}"
                print(f"[{job_id}]   ERROR: {msg}")
                return json.dumps({"ok": False, "error": msg})

            print(
                f"[{job_id}]   normalized data: vehicle={
                    data['vehicleNumber']} "
                f"receipt={data['receiptNumber']} amount={data['amount']} "
                f"date={data['paymentDate']}"
            )

            # ─── 3. Get the active page from the browser session ───
            page = None
            try:
                if hasattr(browser_session, "get_current_page"):
                    page = await browser_session.get_current_page()
                elif hasattr(browser_session, "page"):
                    page = browser_session.page
            except Exception as e:
                msg = f"Could not get active page: {e}"
                print(f"[{job_id}]   ERROR: {msg}")
                return json.dumps({"ok": False, "error": msg})

            if page is None:
                msg = "No active page in browser session"
                print(f"[{job_id}]   ERROR: {msg}")
                return json.dumps({"ok": False, "error": msg})

            # ─── 4. Capture PDF via Chrome DevTools Protocol ───
            cdp = None
            try:
                print(f"[{job_id}]   capturing PDF via CDP Page.printToPDF")
                cdp = await page.context.new_cdp_session(page)
                result = await cdp.send("Page.printToPDF", {
                    "format": "A4",
                    "printBackground": True,
                    "preferCSSPageSize": True,
                    "marginTop": 0.4,
                    "marginBottom": 0.4,
                    "marginLeft": 0.4,
                    "marginRight": 0.4,
                })
            except Exception as e:
                msg = f"CDP printToPDF failed: {str(e)}"
                print(f"[{job_id}]   ERROR: {msg}")
                return json.dumps({"ok": False, "error": msg})
            finally:
                if cdp is not None:
                    try:
                        await cdp.detach()
                    except Exception:
                        # Detach failure is non-fatal; we already have the bytes
                        pass

            pdf_b64 = result.get("data", "")
            if not pdf_b64:
                msg = "CDP printToPDF returned empty data"
                print(f"[{job_id}]   ERROR: {msg}")
                return json.dumps({"ok": False, "error": msg})

            try:
                pdf_bytes = base64.b64decode(pdf_b64)
            except Exception as e:
                msg = f"Failed to decode PDF bytes from CDP response: {e}"
                print(f"[{job_id}]   ERROR: {msg}")
                return json.dumps({"ok": False, "error": msg})

            print(f"[{job_id}]   PDF captured: {len(pdf_bytes)} bytes")

            if len(pdf_bytes) < 1000:
                # Sanity check — a real receipt PDF is at least a few KB.
                # If we got something tiny, the page probably wasn't rendered.
                msg = f"PDF suspiciously small ({
                    len(pdf_bytes)} bytes) — receipt page may not have rendered"
                print(f"[{job_id}]   WARNING: {msg}")
                # Continue anyway — server will validate

            # ─── 5. Send PDF + metadata to API as multipart/form-data ───
            try:
                receipt_no = str(data.get("receiptNumber", "receipt"))
                # Sanitize for filename
                safe_name = "".join(
                    c for c in receipt_no if c.isalnum() or c in "-_"
                )
                filename = f"{
                    safe_name}_receipt.pdf" if safe_name else "receipt.pdf"

                files = {
                    "pdf": (filename, pdf_bytes, "application/pdf"),
                }
                form_data = {
                    "jobId": job_id,
                    "params": json.dumps(job_params),
                    "data": json.dumps(data),
                }

                print(
                    f"[{job_id}]   POSTing multipart to "
                    f"/api/internal/border-tax/save-receipt "
                    f"(pdf={len(pdf_bytes)} bytes, filename={filename})"
                )

                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        f"{API_URL}/api/internal/border-tax/save-receipt",
                        files=files,
                        data=form_data,
                    )

                print(f"[{job_id}] save_receipt response: {resp.status_code}")
                print(f"[{job_id}]   body: {resp.text[:500]}")
                return resp.text
            except Exception as e:
                msg = f"save_receipt HTTP error: {str(e)}"
                print(f"[{job_id}]   ERROR: {msg}")
                return json.dumps({"ok": False, "error": msg})

    # -- dynamic tools from task definition --
    for tool_def in tool_defs:
        _register_dynamic_tool(tools, tool_def, job_id, job_params)

    return tools


def _normalize_data(data) -> list:
    """
    Ensure tool data is a proper list, handling cases where the LLM
    passes a JSON string instead of a parsed list.
    """
    parsedList = []
    if isinstance(data, list):
        parsedList = data

    if isinstance(data, str):
        data = data.strip()
        # Try parsing as JSON string
        try:
            parsed = json.loads(data)
            if isinstance(parsed, list):
                parsedList = parsed
            # Single object wrapped in a string
            if isinstance(parsed, dict):
                parsedList = [parsed]
        except json.JSONDecodeError:
            pass

    if isinstance(data, dict):
        parsedList = [data]

    seen = set()
    deduped = []

    for item in parsedList:
        key = item.get("challanId") if isinstance(item, dict) else None
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(item)
    return deduped


def _register_dynamic_tool(
    tools: Tools,
    tool_def: dict,
    job_id: str,
    job_params: dict,
):
    name = tool_def["name"]
    endpoint = tool_def["endpoint"]
    method = tool_def.get("method", "POST")

    param_lines = []
    for pname, pinfo in tool_def.get("parameters", {}).items():
        param_lines.append(f"  {pname}: {pinfo['description']}")
    param_help = "\n".join(param_lines)

    full_desc = tool_def["description"]
    if param_help:
        full_desc += f"\n\nParameters:\n{param_help}"

    async def handler(data, _endpoint=endpoint, _method=method, _name=name) -> str:
        print(f"[{job_id}] Tool call: {_name}")
        print(f"[{job_id}]   raw data type: {type(data).__name__}")
        print(f"[{job_id}]   raw data preview: {str(data)[:500]}")

        # Normalize: ensure data is always a list
        normalized = _normalize_data(data)
        print(f"[{job_id}]   normalized: {len(normalized)} items")

        if not normalized:
            msg = f"Tool {_name}: no valid data after normalization (raw type={
                type(data).__name__})"
            print(f"[{job_id}]   ERROR: {msg}")
            return json.dumps({"ok": False, "error": msg})

        # Log each item for debugging
        for i, item in enumerate(normalized):
            print(f"[{job_id}]   item[{i}]: {json.dumps(item)
                  if isinstance(item, dict) else str(item)}")

        payload = {
            "jobId": job_id,
            "params": job_params,
            "data": normalized,
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                if _method == "POST":
                    resp = await client.post(f"{API_URL}{_endpoint}", json=payload)
                else:
                    resp = await client.get(f"{API_URL}{_endpoint}",
                                            params={
                                                "payload": json.dumps(payload)}
                                            )

            print(f"[{job_id}] Tool {_name} response: {resp.status_code}")
            print(f"[{job_id}]   body: {resp.text[:500]}")
            return resp.text
        except Exception as e:
            error_msg = f"Tool {_name} HTTP error: {str(e)}"
            print(f"[{job_id}]   ERROR: {error_msg}")
            return json.dumps({"ok": False, "error": error_msg})

    handler.__name__ = name
    handler.__qualname__ = name
    tools.action(description=full_desc)(handler)


async def run_agent(
    prompt: str,
    job_id: str,
    job_params: dict,
    tool_defs: list,
    r: redis.Redis,
    task_id: str = "",
):
    """Returns the raw AgentHistoryList result object (caller extracts final_result and cost)."""
    browser = Browser(
        headless=False,
        chromium_sandbox=False,
        args=["--disable-dev-shm-usage", "--disable-gpu"],
    )

    llm = ChatGoogle(
        model="gemini-2.5-flash",
        vertexai=True,
        location="asia-south1",
        project="cabswale-ai",
    )
    tools = make_tools(job_id, job_params, tool_defs, r, task_id)

    agent = Agent(
        task=prompt,
        llm=llm,
        browser=browser,
        tools=tools,
        calculate_cost=True,
    )

    result = await agent.run(max_steps=100)
    print(f"Token usage: {result.usage}")
    usage_summary = await agent.token_cost_service.get_usage_summary()
    print(f"Usage summary: {usage_summary}")
    return result
