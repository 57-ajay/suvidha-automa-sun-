import Redis from "ioredis";
import { getTask, listTasks } from "./tasks";
import { handleSaveChallans, type InternalRequest } from "./internal/challanSettlement/challans";
import { handleSaveDiscounts } from "./internal/challanSettlement/discounts";
import { handleSaveReceipt } from "./internal/borderTax/receipt";
import { releaseAgentSlot, saveAgentCost } from "./internal/agentConfig";
import { DASHBOARD_HTML } from "./dashboard";

import "./firebase";

const redis = new Redis(process.env.REDIS_URL || "redis://localhost:6379");

const JOB_TTL = 60 * 60 * 24; // 24 hours in seconds
const DEFAULT_PAGE_LIMIT = 100;
const MAX_PAGE_LIMIT = 100;

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
                const { taskId, params, source } = body as
                    { taskId: string; params: Record<string, string>; source?: string };

                if (!taskId) {
                    return Response.json({ error: "taskId required" }, { status: 400 });
                }

                if (source && source !== "web" && source !== "app") {
                    return Response.json({ error: "source can be either web or app" }, { status: 400 });
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
                const now = new Date();
                const createdAt = now.toISOString();
                const createdAtMs = now.getTime();

                const pipeline = redis.pipeline();

                // Store job hash
                pipeline.hset(`job:${jobId}`, {
                    id: jobId,
                    taskId,
                    params: JSON.stringify(params),
                    prompt,
                    tools: JSON.stringify(task.tools ?? []),
                    status: "queued",
                    createdAt,
                    source: source || "web",
                });

                // Set 24H TTL
                pipeline.expire(`job:${jobId}`, JOB_TTL);

                // Index in sorted sets (score = timestamp for ordering)
                pipeline.zadd("jobs:all", createdAtMs, jobId);
                pipeline.zadd(`jobs:task:${taskId}`, createdAtMs, jobId);

                // Push to work queue
                pipeline.lpush("job:queue", jobId);

                await pipeline.exec();

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

            // GET /api/jobs — paginated, filterable by taskId
            if (req.method === "GET" && url.pathname === "/api/jobs") {
                const taskId = url.searchParams.get("taskId") || undefined;
                const page = Math.max(1, parseInt(url.searchParams.get("page") || "1", 10) || 1);
                const limit = Math.min(
                    MAX_PAGE_LIMIT,
                    Math.max(1, parseInt(url.searchParams.get("limit") || String(DEFAULT_PAGE_LIMIT), 10) || DEFAULT_PAGE_LIMIT)
                );

                // Pick the right sorted set
                const indexKey = taskId ? `jobs:task:${taskId}` : "jobs:all";

                // Total count (before pagination)
                const totalCount = await redis.zcard(indexKey);

                // Fetch page (newest first via ZREVRANGE)
                const start = (page - 1) * limit;
                const stop = start + limit - 1;
                const jobIds = await redis.zrevrange(indexKey, start, stop);

                if (jobIds.length === 0) {
                    return Response.json({
                        jobs: [],
                        pagination: { page, limit, total: totalCount, totalPages: Math.ceil(totalCount / limit) },
                    });
                }

                // Pipeline HGETALL for all job IDs
                const pipeline = redis.pipeline();
                for (const jid of jobIds) {
                    pipeline.hgetall(`job:${jid}`);
                }
                const results = await pipeline.exec();

                const jobs: Record<string, string>[] = [];
                const expiredIds: string[] = [];

                for (let i = 0; i < jobIds.length; i++) {
                    const entry = results![i];
                    const err = entry?.[0];
                    const job = entry?.[1] as Record<string, string> | undefined;
                    if (err || !job || !job.id) {
                        // Hash expired (TTL) but sorted set entry remains — mark for cleanup
                        expiredIds.push(jobIds[i]!);
                        continue;
                    }
                    const { prompt, tools, ...rest } = job;
                    jobs.push(rest);
                }

                // Lazy cleanup: remove expired entries from sorted sets
                if (expiredIds.length > 0) {
                    const cleanupPipeline = redis.pipeline();
                    for (const eid of expiredIds) {
                        cleanupPipeline.zrem("jobs:all", eid);
                        // We don't know the taskId of expired jobs, so clean from all task sets
                        // This is fine — ZREM on non-existent members is a no-op
                        const tasks = listTasks();
                        for (const t of tasks) {
                            cleanupPipeline.zrem(`jobs:task:${t}`, eid);
                        }
                    }
                    cleanupPipeline.exec().catch((e) =>
                        console.error("[API] lazy cleanup error:", e)
                    );
                }

                // Adjust total to account for expired entries we just found
                const adjustedTotal = totalCount - expiredIds.length;

                return Response.json({
                    jobs,
                    pagination: {
                        page,
                        limit,
                        total: Math.max(0, adjustedTotal),
                        totalPages: Math.max(1, Math.ceil(Math.max(0, adjustedTotal) / limit)),
                    },
                });
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

            // Border tax receipt save
            if (req.method === "POST" && url.pathname === "/api/internal/border-tax/save-receipt") {
                try {
                    const raw = await req.text();
                    console.log(`[API] POST /api/internal/border-tax/save-receipt | body length=${raw.length}`);
                    console.log(`[API]   body preview: ${raw.substring(0, 500)}`);

                    let body: InternalRequest;
                    try {
                        body = JSON.parse(raw) as InternalRequest;
                    } catch (parseErr) {
                        console.log(`[API]   FAIL: body is not valid JSON`);
                        return Response.json({ ok: false, error: "Request body is not valid JSON" }, { status: 400 });
                    }

                    console.log(`[API]   jobId=${body.jobId} params=${JSON.stringify(body.params)} dataType=${typeof body.data} isArray=${Array.isArray(body.data)}`);

                    const result = await handleSaveReceipt(body);
                    const status = result.ok ? 200 : 400;
                    console.log(`[API]   result: ${JSON.stringify(result)}`);
                    return Response.json(result, { status });
                } catch (e: any) {
                    console.error("[API] ERROR save_receipt:", e);
                    return Response.json({ ok: false, error: e.message }, { status: 500 });
                }
            }

            // POST /api/internal/job-completed - worker calls this fire-and-forget when a job finishes
            if (req.method === "POST" && url.pathname === "/api/internal/job-completed") {
                try {
                    const body = await req.json() as {
                        jobId: string;
                        requestId?: string;
                        costData?: Record<string, any>;
                        source?: string;
                    };
                    const { jobId, requestId, costData, source } = body;

                    console.log(`[API] POST /api/internal/job-completed | jobId=${jobId} requestId=${requestId} hasCost=${!!costData}`);

                    // Refresh TTL on completion so job stays visible for 24H from now
                    await redis.expire(`job:${jobId}`, JOB_TTL);

                    if (!requestId) {
                        console.log(`[API]   no requestId, skipping agent config release`);
                        return Response.json({ ok: true, skipped: true });
                    }

                    // Release agent slot (fire-and-forget)
                    releaseAgentSlot(jobId).catch((e) => {
                        console.error(`[API] background releaseAgentSlot failed for requestId=${requestId}:`, e);
                    });

                    // Save cost data to challanRequest (fire-and-forget)
                    if (costData) {
                        saveAgentCost(requestId, jobId, costData, source).catch((e) => {
                            console.error(`[API] background saveAgentCost failed for requestId=${requestId}:`, e);
                        });
                    }

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

                // Refresh TTL so cancelled job stays visible for 24H
                await redis.expire(`job:${jobId}`, JOB_TTL);

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
