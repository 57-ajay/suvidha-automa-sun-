import asyncio
import json
import os

import httpx
import redis
from browser_use import Agent, Browser, ChatGoogle, Tools

API_URL = os.environ.get("API_URL", "http://api:3000")


def make_tools(job_id: str, job_params: dict, tool_defs: list, r: redis.Redis) -> Tools:
    tools = Tools()

    # -- always available: wait_for_human --
    @tools.action(
        description=(
            "Call this when you need human help. "
            "Pass a reason (e.g. 'OTP required', 'CAPTCHA needs solving'). "
            "The human can interact with the browser directly via the live view, "
            "or send a text response via API. "
            "Returns the human's response when they are finished."
        )
    )
    async def wait_for_human(reason: str) -> str:
        print(f"[{job_id}] Waiting for human: {reason}")

        r.hset(f"job:{job_id}", mapping={
            "status": "waiting_for_human",
            "waitReason": reason,
        })

        while True:
            human_input = r.hget(f"job:{job_id}", "humanInput")
            if human_input:
                human_input = human_input.decode() if isinstance(
                    human_input, bytes) else human_input
                r.hdel(f"job:{job_id}", "humanInput", "waitReason")
                r.hset(f"job:{job_id}", "status", "running")
                print(f"[{job_id}] Human done: {human_input}")
                return human_input
            await asyncio.sleep(1)

    # -- dynamic tools from task definition --
    for tool_def in tool_defs:
        _register_dynamic_tool(tools, tool_def, job_id, job_params)

    return tools


def _normalize_data(data) -> list:
    """
    Ensure tool data is a proper list, handling cases where the LLM
    passes a JSON string instead of a parsed list.
    """
    if isinstance(data, list):
        return data

    if isinstance(data, str):
        data = data.strip()
        # Try parsing as JSON string
        try:
            parsed = json.loads(data)
            if isinstance(parsed, list):
                return parsed
            # Single object wrapped in a string
            if isinstance(parsed, dict):
                return [parsed]
        except json.JSONDecodeError:
            pass

    if isinstance(data, dict):
        return [data]

    return []


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


async def run_agent(prompt: str, job_id: str, job_params: dict, tool_defs: list, r: redis.Redis) -> str:
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
    tools = make_tools(job_id, job_params, tool_defs, r)

    agent = Agent(
        task=prompt,
        llm=llm,
        browser=browser,
        tools=tools,
    )

    result = await agent.run(max_steps=100)
    return result.final_result() or "No result returned"
