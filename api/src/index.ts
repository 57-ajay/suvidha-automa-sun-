import Redis from "ioredis";
import { getTask, listTasks } from "./tasks";
import { handleSaveChallans, type InternalRequest } from "./internal/challanSettlement/challans";
import { handleSaveDiscounts } from "./internal/challanSettlement/discounts";
import { releaseAgentSlot } from "./internal/agentConfig";
import { DASHBOARD_HTML } from "./dashboard";

import "./firebase";

const redis = new Redis(process.env.REDIS_URL || "redis://localhost:6379");

function corsHeaders(): Record<string, string> {
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    };
}

const server = Bun.serve({
    port: 3000,
    async fetch(req) {
        // CORS preflight
        if (req.method === "OPTIONS") {
            return new Response(null, { status: 204, headers: corsHeaders() });
        }

        const res = await (async (): Promise<Response> => {
            const url = new URL(req.url);

            // POST /api/run
            if (req.method === "POST" && url.pathname === "/api/run") {
                const body = await req.json();
                const { taskId, params } = body as { taskId: string; params: Record<string, string> };

                if (!taskId) {
                    return Response.json({ error: "taskId required" }, { status: 400 });
                }

                const task = getTask(taskId);
                if (!task) {
                    return Response.json(
                        { error: `Unknown task: ${taskId}`, available: listTasks() },
                        { status: 400 }
                    );
                }

                const missing = task.requiredParams.filter(
                    (p) => !params || !params[p]
                );
                if (missing.length > 0) {
                    return Response.json(
                        { error: `Missing params: ${missing.join(", ")}` },
                        { status: 400 }
                    );
                }

                const prompt = await task.buildPrompt(params);
                const jobId = crypto.randomUUID();

                await redis.hset(`job:${jobId}`, {
                    id: jobId,
                    taskId,
                    params: JSON.stringify(params),
                    prompt,
                    tools: JSON.stringify(task.tools ?? []),
                    status: "queued",
                    createdAt: new Date().toISOString(),
                });

                await redis.lpush("job:queue", jobId);

                return Response.json({ jobId });
            }

            // GET /api/tasks
            if (req.method === "GET" && url.pathname === "/api/tasks") {
                return Response.json({ tasks: listTasks() });
            }

            // GET /api/jobs/:id/status
            const statusMatch = url.pathname.match(/^\/api\/jobs\/([^/]+)\/status$/);
            if (req.method === "GET" && statusMatch) {
                const jobId = statusMatch[1];
                const job = await redis.hgetall(`job:${jobId}`);

                if (!job || !job.id) {
                    return Response.json({ error: "not found" }, { status: 404 });
                }

                const { prompt, tools, ...rest } = job;

                let mobileNumber: string | undefined;
                try {
                    const parsedParams = JSON.parse(job.params || "{}");
                    if (parsedParams.mobileNumber) {
                        mobileNumber = parsedParams.mobileNumber;
                    }
                } catch { }

                return Response.json({
                    ...rest,
                    ...(mobileNumber ? { mobileNumber } : {}),
                });
            }

            // POST /api/jobs/:id/intervene
            const interveneMatch = url.pathname.match(
                /^\/api\/jobs\/([^/]+)\/intervene$/
            );
            if (req.method === "POST" && interveneMatch) {
                const jobId = interveneMatch[1];
                const job = await redis.hgetall(`job:${jobId}`);

                if (!job || !job.id) {
                    return Response.json({ error: "not found" }, { status: 404 });
                }

                if (job.status !== "waiting_for_human") {
                    return Response.json(
                        { error: "Job is not waiting for human input", status: job.status },
                        { status: 400 }
                    );
                }

                const body = await req.json();
                const { input } = body as { input: any };

                if (!input) {
                    return Response.json({ error: "input required" }, { status: 400 });
                }

                await redis.hset(`job:${jobId}`, "humanInput", input);

                return Response.json({ ok: true, message: "Input submitted, agent will resume" });
            }

            // GET /api/jobs
            if (req.method === "GET" && url.pathname === "/api/jobs") {
                const keys = await redis.keys("job:*");
                const jobs = [];

                for (const key of keys) {
                    if (key === "job:queue") continue;
                    const type = await redis.type(key);
                    if (type !== "hash") continue;

                    const job = await redis.hgetall(key);
                    if (job && job.id) {
                        const { prompt, tools, ...rest } = job;
                        jobs.push(rest);
                    }
                }

                jobs.sort((a, b) => (b.createdAt || "").localeCompare(a.createdAt || ""));

                return Response.json({ jobs });
            }

            // Internal endpoints (called by worker tools)

            if (req.method === "POST" && url.pathname === "/api/internal/challans/save") {
                try {
                    const raw = await req.text();
                    console.log(`[API] POST /api/internal/challans/save | body length=${raw.length}`);
                    console.log(`[API]   body preview: ${raw.substring(0, 500)}`);

                    let body: InternalRequest;
                    try {
                        body = JSON.parse(raw) as InternalRequest;
                    } catch (parseErr) {
                        console.log(`[API]   FAIL: body is not valid JSON`);
                        return Response.json({ ok: false, error: "Request body is not valid JSON" }, { status: 400 });
                    }

                    console.log(`[API]   jobId=${body.jobId} params=${JSON.stringify(body.params)} dataType=${typeof body.data} isArray=${Array.isArray(body.data)}`);

                    const result = await handleSaveChallans(body);
                    const status = result.ok ? 200 : 400;
                    console.log(`[API]   result: ${JSON.stringify(result)}`);
                    return Response.json(result, { status });
                } catch (e: any) {
                    console.error("[API] ERROR save_challans:", e);
                    return Response.json({ ok: false, error: e.message }, { status: 500 });
                }
            }

            if (req.method === "POST" && url.pathname === "/api/internal/discounts/save") {
                try {
                    const raw = await req.text();
                    console.log(`[API] POST /api/internal/discounts/save | body length=${raw.length}`);
                    console.log(`[API]   body preview: ${raw.substring(0, 500)}`);

                    let body: InternalRequest;
                    try {
                        body = JSON.parse(raw) as InternalRequest;
                    } catch (parseErr) {
                        console.log(`[API]   FAIL: body is not valid JSON`);
                        return Response.json({ ok: false, error: "Request body is not valid JSON" }, { status: 400 });
                    }

                    console.log(`[API]   jobId=${body.jobId} params=${JSON.stringify(body.params)} dataType=${typeof body.data} isArray=${Array.isArray(body.data)}`);

                    const result = await handleSaveDiscounts(body);
                    const status = result.ok ? 200 : 400;
                    console.log(`[API]   result: ${JSON.stringify(result)}`);
                    return Response.json(result, { status });
                } catch (e: any) {
                    console.error("[API] ERROR save_discounts:", e);
                    return Response.json({ ok: false, error: e.message }, { status: 500 });
                }
            }

            // POST /api/internal/job-completed - worker calls this fire-and-forget when a job finishes
            if (req.method === "POST" && url.pathname === "/api/internal/job-completed") {
                try {
                    const body = await req.json() as { jobId: string; requestId?: string };
                    const { jobId, requestId } = body;

                    console.log(`[API] POST /api/internal/job-completed | jobId=${jobId} requestId=${requestId}`);

                    if (!requestId) {
                        console.log(`[API]   no requestId, skipping agent config release`);
                        return Response.json({ ok: true, skipped: true });
                    }

                    releaseAgentSlot(jobId).catch((e) => {
                        console.error(`[API] background releaseAgentSlot failed for requestId=${requestId}:`, e);
                    });

                    return Response.json({ ok: true });
                } catch (e: any) {
                    console.error("[API] ERROR job-completed:", e);
                    return Response.json({ ok: false, error: e.message }, { status: 500 });
                }
            }

            // POST /api/jobs/:id/cancel
            const cancelMatch = url.pathname.match(/^\/api\/jobs\/([^/]+)\/cancel$/);
            if (req.method === "POST" && cancelMatch) {
                const jobId = cancelMatch[1]!;
                const job = await redis.hgetall(`job:${jobId}`);

                if (!job || !job.id) {
                    return Response.json({ error: "not found" }, { status: 404 });
                }

                const current = job.status;
                if (current === "done" || current === "failed" || current === "cancelled") {
                    return Response.json(
                        { error: `Job already ${current}` },
                        { status: 400 }
                    );
                }

                if (current === "queued") {
                    await releaseAgentSlot(jobId);
                    await redis.lrem("job:queue", 0, jobId);
                }

                await redis.hset(`job:${jobId}`, "status", "cancelled");
                await releaseAgentSlot(jobId);

                return Response.json({ ok: true, message: "Cancellation requested" });
            }

            // GET /api/dashboard
            if (req.method === "GET" && url.pathname === "/api/dashboard") {
                return new Response(DASHBOARD_HTML, {
                    headers: { "Content-Type": "text/html" },
                });
            }

            return Response.json({ error: "not found" }, { status: 404 });
        })();

        // Attach CORS headers to every response
        for (const [k, v] of Object.entries(corsHeaders())) {
            res.headers.set(k, v);
        }
        return res;
    },
});

console.log(`API running on :${server.port}`);
