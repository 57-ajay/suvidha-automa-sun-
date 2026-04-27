import type { ServerWebSocket } from "bun";
import Redis from "ioredis";

interface WsCtx {
    workerId: string;
    upstream: WebSocket;
    upstreamReady: boolean;
    queued: (string | ArrayBuffer)[];
}

const HOP_BY_HOP = new Set([
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "proxy-authorization",
    "proxy-authenticate",
    "te",
    "trailer",
]);

/**
 * Look up which worker holds the live view for a given jobId.
 * Returns null if no routing entry exists (job not running, expired, etc.).
 */
async function resolveWorker(redis: Redis, jobId: string): Promise<{ ip: string; port: string } | null> {
    const routing = await redis.get(`worker:job:${jobId}`);
    if (!routing) return null;

    const [workerId] = routing.split(":");
    if (!workerId) return null;

    const meta = await redis.hgetall(`worker:${workerId}:meta`);
    if (!meta || !meta.ip || !meta.port) return null;

    return { ip: meta.ip, port: meta.port };
}

/**
 * Strip the /vnc/{jobId} prefix from a path.
 * `/vnc/abc-123/core/util.js` -> `/core/util.js`
 * `/vnc/abc-123/websockify` -> `/websockify`
 */
function stripPrefix(pathname: string, jobId: string): string {
    const prefix = `/vnc/${jobId}`;
    if (pathname === prefix) return "/";
    if (pathname.startsWith(prefix + "/")) return pathname.slice(prefix.length);
    return pathname;
}

function parseJobId(pathname: string): string | null {
    const m = pathname.match(/^\/vnc\/([^/]+)/);
    return m ? m[1]! : null;
}

/**
 * HTTP-side handler: proxies static noVNC assets to the worker's websockify HTTP server.
 */
export async function handleVncHttp(
    req: Request,
    redis: Redis,
): Promise<Response> {
    const url = new URL(req.url);
    const jobId = parseJobId(url.pathname);
    if (!jobId) return new Response("bad vnc path", { status: 400 });

    const worker = await resolveWorker(redis, jobId);
    if (!worker) {
        return new Response("live view unavailable (job not running)", { status: 404 });
    }

    const upstreamPath = stripPrefix(url.pathname, jobId);
    const upstreamUrl = `http://${worker.ip}:${worker.port}${upstreamPath}${url.search}`;

    const headers = new Headers();
    for (const [k, v] of req.headers) {
        if (!HOP_BY_HOP.has(k.toLowerCase()) && k.toLowerCase() !== "host") {
            headers.set(k, v);
        }
    }

    try {
        const upstream = await fetch(upstreamUrl, {
            method: req.method,
            headers,
            body: req.method === "GET" || req.method === "HEAD" ? undefined : req.body,
            // @ts-ignore  Bun-specific
            redirect: "manual",
        });
        return new Response(upstream.body, {
            status: upstream.status,
            statusText: upstream.statusText,
            headers: upstream.headers,
        });
    } catch (e: any) {
        console.error(`[vncProxy] upstream HTTP fetch failed for ${jobId}:`, e);
        return new Response("upstream worker unreachable", { status: 502 });
    }
}

/**
 * Decide whether a request is a WebSocket upgrade for /vnc/*.
 * Returns the resolved upstream URL if so, or null.
 */
export async function tryUpgradeVnc(
    req: Request,
    redis: Redis,
): Promise<{ jobId: string; upstreamUrl: string; ctx: Omit<WsCtx, "upstream" | "upstreamReady" | "queued"> } | null> {
    const upgrade = req.headers.get("upgrade");
    if (!upgrade || upgrade.toLowerCase() !== "websocket") return null;

    const url = new URL(req.url);
    const jobId = parseJobId(url.pathname);
    if (!jobId) return null;

    const worker = await resolveWorker(redis, jobId);
    if (!worker) return null;

    const upstreamPath = stripPrefix(url.pathname, jobId);
    const upstreamUrl = `ws://${worker.ip}:${worker.port}${upstreamPath}${url.search}`;

    return {
        jobId,
        upstreamUrl,
        ctx: { workerId: `${worker.ip}:${worker.port}` },
    };
}

/**
 * Bun WebSocket message handlers. We pipe in both directions:
 * browser <-> edge <-> worker websockify <-> x11vnc.
 */
export const vncWebSocketHandlers = {
    open(ws: ServerWebSocket<WsCtx>) {
        const upstream = ws.data.upstream;

        upstream.binaryType = "arraybuffer";

        upstream.addEventListener("open", () => {
            ws.data.upstreamReady = true;
            for (const m of ws.data.queued) {
                upstream.send(m as any);
            }
            ws.data.queued = [];
        });

        upstream.addEventListener("message", (ev: MessageEvent) => {
            try {
                ws.send(ev.data as any);
            } catch (e) {
                console.error("[vncProxy] downstream send error:", e);
            }
        });

        upstream.addEventListener("close", (ev: CloseEvent) => {
            try { ws.close(ev.code || 1000, ev.reason || "upstream closed"); } catch { }
        });

        upstream.addEventListener("error", (e) => {
            console.error("[vncProxy] upstream ws error:", e);
            try { ws.close(1011, "upstream error"); } catch { }
        });
    },

    message(ws: ServerWebSocket<WsCtx>, message: string | ArrayBuffer | Uint8Array) {
        const upstream = ws.data.upstream;
        // Normalize Uint8Array -> ArrayBuffer for upstream.send signature
        const m = message instanceof Uint8Array
            ? message.buffer.slice(message.byteOffset, message.byteOffset + message.byteLength)
            : message;

        if (!ws.data.upstreamReady) {
            ws.data.queued.push(m as any);
            return;
        }
        try {
            upstream.send(m as any);
        } catch (e) {
            console.error("[vncProxy] upstream send error:", e);
        }
    },

    close(ws: ServerWebSocket<WsCtx>) {
        try { ws.data.upstream.close(1000, "client closed"); } catch { }
    },
};
