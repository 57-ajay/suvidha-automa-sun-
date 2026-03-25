import Redis from "ioredis";
import { getTask, listTasks } from "./tasks";
import { handleSaveChallans, type InternalRequest } from "./internal/challans";
import { handleSaveDiscounts } from "./internal/discounts";
import { DASHBOARD_HTML } from "./dashboard";

import "./firebase";

const redis = new Redis(process.env.REDIS_URL || "redis://localhost:6379");

const server = Bun.serve({
    port: 3000,
    async fetch(req) {
        const url = new URL(req.url);

        // POST /api/run
        if (req.method === "POST" && url.pathname === "/api/run") {
            const body = await req.json();
            const { taskId, params } = body as { taskId: string; params: any };

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

            const prompt = task.buildPrompt(params);
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
            return Response.json(rest);
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
                const body = await req.json() as InternalRequest;
                const result = await handleSaveChallans(body);
                const status = result.ok ? 200 : 400;
                return Response.json(result, { status });
            } catch (e: any) {
                console.error("[ERROR] save_challans:", e);
                return Response.json({ ok: false, error: e.message }, { status: 500 });
            }
        }

        if (req.method === "POST" && url.pathname === "/api/internal/discounts/save") {
            try {
                const body = await req.json() as InternalRequest;
                const result = await handleSaveDiscounts(body);
                const status = result.ok ? 200 : 400;
                return Response.json(result, { status });
            } catch (e: any) {
                console.error("[ERROR] save_discounts:", e);
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
                await redis.lrem("job:queue", 0, jobId);
            }

            await redis.hset(`job:${jobId}`, "status", "cancelled");

            return Response.json({ ok: true, message: "Cancellation requested" });
        }

        // GET /api/dashboard
        if (req.method === "GET" && url.pathname === "/api/dashboard") {
            return new Response(DASHBOARD_HTML, {
                headers: { "Content-Type": "text/html" },
            });
        }

        return Response.json({ error: "not found" }, { status: 404 });
    },
});

console.log(`API running on :${server.port}`);
