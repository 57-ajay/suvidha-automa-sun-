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

    async def handler(data: str, _endpoint=endpoint, _method=method, _name=name) -> str:
        print(f"[{job_id}] Tool call: {_name}")
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"Invalid JSON: {e}"})

        payload = {
            "jobId": job_id,
            "params": job_params,
            "data": parsed,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            if _method == "POST":
                resp = await client.post(f"{API_URL}{_endpoint}", json=payload)
            else:
                resp = await client.get(f"{API_URL}{_endpoint}", params={"payload": json.dumps(payload)})

        print(f"[{job_id}] Tool {_name} response: {resp.status_code}")
        return resp.text

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

    result = await agent.run()
    return result.final_result() or "No result returned"
