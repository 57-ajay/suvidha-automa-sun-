"""AI-driven agent runner.

The legacy/general-purpose path. Still used for:
  - Non-border-tax tasks (challan settlement, etc.)
  - border-tax states/payment-methods where the scripted runner isn't
    enabled (SCRIPTED_BORDER_TAX_STATES env var doesn't include the state,
    or paymentMethod=net_banking which v1 scripted doesn't support).

After the chunk-2 refactor:
  - Tool decorators still register the same tools with browser-use Agent.
  - Tool BODIES are now one-line calls into actions.py. The actual CDP /
    Redis / HTTP logic lives there once and is shared with the scripted
    path.
  - Behavior on the wire is unchanged -- tools still return JSON strings
    with the same shapes the LLM has been seeing.
"""

import json
import os

import httpx
import redis
from browser_use import Agent, Browser, BrowserSession, ChatGoogle, Tools

from actions import (
    HUMAN_WAIT_TIMEOUT,
    save_qr_code as _save_qr_code_impl,
    save_receipt as _save_receipt_impl,
    wait_for_human as _wait_for_human_impl,
)


API_URL = os.environ.get("API_URL", "http://api:3000")


def make_tools(
    job_id: str,
    job_params: dict,
    tool_defs: list,
    r: redis.Redis,
    task_id: str = "",
) -> Tools:
    tools = Tools()

    # ─── wait_for_human (always available) ────────────────────────────
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
        return await _wait_for_human_impl(job_id, r, reason)

    # ─── border-tax tools ─────────────────────────────────────────────
    if task_id == "border-tax":

        @tools.action(
            description=(
                "Call this ONCE when the UPI QR code page is fully loaded and visible, "
                "BEFORE calling wait_for_human. "
                "This tool extracts the QR code image from the current page, "
                "uploads it to cloud storage, and saves the URL on the request record "
                "so the client app can display the QR directly to the user.\n\n"
                "Takes NO parameters — call it as save_qr_code({}).\n\n"
                "Returns JSON:\n"
                '  {"ok": true}  → QR uploaded. Proceed to call wait_for_human as normal.\n'
                '  {"ok": false} → Upload failed. Log the error, then STILL call '
                "wait_for_human — a QR upload failure must NEVER block the payment.\n\n"
                "IMPORTANT: Call this at most ONCE per job. Do NOT retry on failure."
            )
        )
        async def save_qr_code(browser_session: BrowserSession) -> str:
            result = await _save_qr_code_impl(browser_session, job_id, job_params)
            return json.dumps(result)

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
                '{"vehicleNumber":"HR55AZ1101",'
                '"receiptNumber":"UPR2604280468752",'
                '"amount":120,'
                '"paymentDate":"2026-04-28"}\n\n'
                'Returns JSON. Confirm both "ok": true AND '
                '"pdfUploaded": true to consider the call fully successful. '
                'If "ok": false, do NOT retry — record the error and complete '
                "with 'Status: partial'."
            )
        )
        async def save_receipt(data, browser_session: BrowserSession) -> str:
            result = await _save_receipt_impl(browser_session, job_id, job_params, data)
            return json.dumps(result)

    # ─── dynamic tools from task definition ───────────────────────────
    for tool_def in tool_defs:
        _register_dynamic_tool(tools, tool_def, job_id, job_params)

    return tools


def _normalize_data(data) -> list:
    """Ensure tool data is a proper list, handling cases where the LLM
    passes a JSON string instead of a parsed list."""
    parsedList = []
    if isinstance(data, list):
        parsedList = data

    if isinstance(data, str):
        data = data.strip()
        try:
            parsed = json.loads(data)
            if isinstance(parsed, list):
                parsedList = parsed
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

        normalized = _normalize_data(data)
        print(f"[{job_id}]   normalized: {len(normalized)} items")

        if not normalized:
            msg = (
                f"Tool {_name}: no valid data after normalization "
                f"(raw type={type(data).__name__})"
            )
            print(f"[{job_id}]   ERROR: {msg}")
            return json.dumps({"ok": False, "error": msg})

        for i, item in enumerate(normalized):
            print(
                f"[{job_id}]   item[{i}]: "
                f"{json.dumps(item) if isinstance(item, dict) else str(item)}"
            )

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
                    resp = await client.get(
                        f"{API_URL}{_endpoint}",
                        params={"payload": json.dumps(payload)},
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
    """Returns the raw AgentHistoryList result object (caller extracts
    final_result and cost)."""
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
    try:
        cached = usage_summary.total_prompt_cached_tokens or 0
        total = usage_summary.total_prompt_tokens or 1
        print(f"Cache hit rate: {cached}/{total} = {100 * cached / total:.1f}%")
    except Exception:
        pass
    return result
