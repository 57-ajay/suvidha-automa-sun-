# Suvidha Automa — System Documentation

A system that **automates Indian government portal payments** using browser automation
driven by AI agents. Its main job: paying **inter-state "border tax"** for vehicles on the
`parivahan.gov.in` checkpost portal, plus a secondary flow for **traffic challan (fine)
settlement** on Delhi Traffic Police / Virtual Courts portals.

Think of it as a fleet of robots that log into clunky government websites, fill multi-page
forms, solve CAPTCHAs, complete payments, and download receipts — at scale, with humans able
to watch and step in via remote browser viewing.

---

## 1. Deployment / Container Architecture

What actually runs, from `docker-compose.yaml` + `Caddyfile`:

```
                          Internet
                             │
                             ▼  :80 / :443
                  ┌──────────────────────┐
                  │        CADDY          │   reverse proxy
                  │  {$DOMAIN}            │
                  └──────────┬───────────┘
                 /api/*      │      /*  (everything else)
              ┌──────────────┘──────────────┐
              ▼                              ▼
   ┌─────────────────┐            ┌────────────────────┐
   │   API (Bun/TS)  │            │  WORKER (Python)    │
   │   :3000         │            │  noVNC :6080        │
   │                 │            │  shm_size 4gb       │
   └────────┬────────┘            └─────────┬──────────┘
            │                               │
            │      ┌─────────────┐          │
            └─────►│   REDIS     │◄─────────┘
                   │  :6379      │   job queue + state
                   └─────────────┘
            │                               │
            ▼                               ▼
   ┌─────────────────┐            ┌────────────────────┐
   │  FIREBASE       │            │  CHROMIUM × N       │
   │  Firestore + GCS│            │  (one per job slot, │
   │  bwi-cabswalle  │            │   Xvfb + x11vnc)    │
   └─────────────────┘            └────────────────────┘
```

- **Caddy** splits traffic: API calls vs. the live browser-viewing (noVNC) traffic.
- **Redis** is the *only* thing the API and worker share directly — fully decoupled.
- **API** owns persistence (Firebase). **Worker** owns the browsers.

### The two services

**API service (`api/`, TypeScript + Bun)** — the orchestrator. Accepts jobs, builds the
LLM prompt, pushes the job to Redis, receives callbacks, persists to Firebase (Firestore +
Cloud Storage, project `bwi-cabswalle`), and enforces per-driver rate limits.

**Worker service (`worker/`, Python + uv)** — the executor. Pulls jobs from Redis, runs up
to `MAX_SLOTS` (default 10) concurrently, gives each its own virtual display (Xvfb) + VNC
(x11vnc) viewable via noVNC on port 6080, then routes the job down one of two execution paths.

---

## 2. Job Lifecycle (end to end)

```
 CLIENT            API (Bun)              REDIS            WORKER (Python)         FIREBASE/GCS
   │                  │                     │                    │                     │
   │ POST /api/run    │                     │                    │                     │
   │  {taskId,params} │                     │                    │                     │
   ├─────────────────►│                     │                    │                     │
   │                  │ dedupe? eligibility?│                    │                     │
   │                  │ preprocessParams()  │                    │                     │
   │                  │ buildPrompt()       │                    │                     │
   │                  │                     │                    │                     │
   │                  │ HSET job:{id}       │                    │                     │
   │                  │ LPUSH job:queue ────►│                    │                     │
   │   {jobId}        │ (status=queued)     │                    │                     │
   │◄─────────────────┤                     │                    │                     │
   │                  │ ░ fire-and-forget:  │                    │                     │
   │                  │   assignedPartner,  │   set status "started" ───────────────────►│
   │                  │                     │                    │                     │
   │                  │                     │◄── BRPOP job:queue ─┤  (loop, MAX_SLOTS)  │
   │                  │                     │                    │ acquire slot:       │
   │                  │                     │                    │  Xvfb + x11vnc      │
   │                  │                     │                    │  spawn run_job.py   │
   │                  │                     │                    │                     │
   │                  │                     │                    │ ROUTE: AI / scripted│
   │                  │                     │                    │ drive Chromium ───► (govt portal)
   │                  │                     │                    │                     │
   │ GET /jobs/:id/   │                     │                    │                     │
   │     status       │ HGETALL job:{id} ◄──┤                    │                     │
   │◄────────────────►│                     │                    │                     │
   │ (poll: queued →  │                     │                    │                     │
   │  running → ...)  │                     │                    │                     │
   │                  │                     │                    │ ── callback: save-qr / save-receipt ──►
   │                  │ POST /internal/...  │◄───────────────────┤   (multipart PDF/PNG → GCS)         │
   │                  │ upload to GCS ──────┼────────────────────┼─────────────────────────────────────►│
   │                  │                     │                    │                     │
   │                  │                  ┌──┴── if waiting_for_human ──┐                │
   │ POST /intervene  │                  │  status=waiting_for_human   │                │
   │  {input:OTP}     │ HSET humanInput ─►│  worker polls humanInput   │                │
   │─────────────────►│                  └──┬──────────────────────────┘                │
   │                  │                     │  ◄── human watches via noVNC :6080         │
   │                  │                     │                    │                     │
   │                  │                     │                    │ DONE → notify       │
   │                  │ POST /internal/     │◄───────────────────┤                     │
   │                  │   job-completed     │                    │ release slot        │
   │                  │ release agent slot, save runLog,         │                     │
   │                  │ work summary, cost, usage counters ──────────────────────────► │
   │                  │ refresh TTL (24h) ──►│                    │                     │
```

### Job status state machine

```
queued ──► running ──┬──────────────► done
                     ├──► waiting_for_human ──► running ──► done
                     ├──────────────► partial
                     ├──────────────► failed
                     └── (cancel) ──► cancelled
```

### Redis data structures

| Key                       | Type        | Purpose                                    |
|---------------------------|-------------|--------------------------------------------|
| `job:{id}`                | hash        | One per job: prompt, params, status, etc.  |
| `job:queue`               | list        | FIFO queue of jobIds awaiting pickup       |
| `jobs:all`                | sorted set  | All jobIds scored by creation time         |
| `jobs:task:{taskId}`      | sorted set  | jobIds filtered by task                    |
| `job:{id}:steps`          | list        | Live scripted step log (RPUSH)             |

Job hashes get a **24-hour TTL**, refreshed on completion.

---

## 3. The Routing Decision (AI vs Scripted vs Human handover)

This is the heart of the design. From `worker/src/run_job.py`:

```
                  job picked up
                       │
                       ▼
            ┌──────────────────────┐
            │  taskId?              │
            └──────────────────────┘
              │          │           │
   fetch-receipt    border-tax    challan-settlement / test-human
       │                │                     │
       ▼                ▼                     ▼
   SCRIPTED      ┌─────────────┐          AI PATH
   (always)      │ state in    │       (browser-use
                 │ SCRIPTED_   │        + Gemini LLM)
                 │ BORDER_TAX_ │
                 │ STATES ?    │
                 └─────────────┘
                  yes │     │ no
                      ▼     └────────► AI PATH
              ┌───────────────┐
              │ source / state│
              │ net-banking?  │
              └───────────────┘
               │            │
         UPI states    net-banking states (UK/HP/BR)
         (UP HR PB      → SCRIPTED + WEB HANDOVER
          MP TN RJ)       (human pays in the live
         → SCRIPTED        browser via noVNC; script
                           polls for the receipt)
```

Two env vars steer everything:

- `SCRIPTED_BORDER_TAX_STATES` — which states bypass the LLM (e.g.
  `"UP,HR,RJ,PB,MP,UK,HP,BR,TN"`). Empty ⇒ everything uses the AI path.
- `SCRIPTED_BORDER_TAX_AUTO_CLEAR_PENDING` — whether to auto-resolve stale "pending
  transaction" popups.

### The two execution paths

**1. AI path** (`agent.py`) — uses `browser-use` + a Gemini LLM to *intelligently* drive the
browser. Flexible, handles surprises, but costs money per token and is slower / less
predictable.

**2. Scripted path** (`scripted/`) — hand-coded, deterministic step sequences using Chrome
DevTools Protocol. Fast, cheap, reliable. This is the direction the project is moving.

The hybrid model: scripted flows run deterministically, but when a step hits something it
can't handle, it raises `HandoffNeeded` and spins up a *narrowly-scoped* AI agent (max ~15
steps) to rescue just that step, then control returns to the deterministic script.

---

## 4. The Border-Tax Scripted Flow (6 phases)

Every state runner (`worker/src/scripted/border_tax/up.py`, etc.) follows this shape on
`parivahan.gov.in`:

```
  ┌──────────────────────────────────────────────────────────┐
  │ PHASE 1  Navigate portal → select STATE from dropdown      │
  ├──────────────────────────────────────────────────────────┤
  │ PHASE 2  Select "VEHICLE TAX COLLECTION (OTHER STATE)"     │
  ├──────────────────────────────────────────────────────────┤
  │ PHASE 3  Owner Info form (vehicle, district, checkpoint)   │
  │          │                                                 │
  │          └─ "pending transaction" popup? ──┐               │
  │             (if AUTO_CLEAR_PENDING=true)    ▼               │
  │             _pending_clear.py: go to "Check Pending",      │
  │             solve captcha, click bank icon, poll outcome:  │
  │               cleared    → restart phases 1-3              │
  │               on_hold    → abort ("try after X min")       │
  │               failed     → abort                           │
  ├──────────────────────────────────────────────────────────┤
  │ PHASE 4  Vehicle Info (permit type, service type)          │
  ├──────────────────────────────────────────────────────────┤
  │ PHASE 5  Tax Info (mode, from, upto)                       │
  │          _extract_amount.py: read tax table, sum rows,     │
  │          save borderTaxAmount → Redis (best-effort)        │
  ├──────────────────────────────────────────────────────────┤
  │ PHASE 6  PAYMENT  ── splits by payment method ──┐          │
  └──────────────────────────────────────────────────────────┘
                │                    │                  │
        UPI (PB/MP/TN)        net-banking + AI    net-banking, no AI
                │              (UP/RJ)            (UK/HP/BR)
                ▼                    ▼                  ▼
        save QR (save_qr_code) | captcha + pay     WEB HANDOVER
        human pays via UPI     | via agent         _web_handover.py
                │                    │             human pays in noVNC
                └────────┬───────────┴──────────────────┘
                         ▼
              _payment_wait.py  (Phase A/B/C)
              A: poll 180s for "payment successful" / "failed"
              B: 60s verify, click "click here", look for receipt
              C: extract receipt fields → save_receipt() → GCS
                         │
                         ▼
                  done / partial / failed
```

### Shared scripted helpers (`worker/src/scripted/border_tax/`)

| Helper                | Responsibility                                                       |
|-----------------------|----------------------------------------------------------------------|
| `_extract_amount.py`  | Read the Tax/Fee table, sum row amounts, save to Redis (best-effort) |
| `_payment_wait.py`    | Phase A/B/C: wait for payment signal, verify, capture receipt        |
| `_web_handover.py`    | Human pays in live browser; concurrent QR + receipt polling          |
| `_pending_clear.py`   | Auto-clear stale "pending transaction" popups (UP/HR/PB/MP)          |
| `_handover_runner.py` | Form-fill-only flow for states with no AI at all (HP/BR)             |

---

## 5. CAPTCHA Escalation Ladder

Cost-optimized — cheapest method first, human last (`worker/src/scripted/captcha.py`):

```
   canvas.toDataURL('image/png')   ← grab the captcha image
              │
              ▼
   ┌────────────────────────┐
   │ 1. Direct LLM OCR       │  ~$0.0001   (Vertex Gemini, one shot)
   └────────────────────────┘
              │ fail / wrong
              ▼
   ┌────────────────────────┐
   │ 2. Full browser-use     │  ~$0.015    (agent looks & types)
   │    agent rescue         │
   └────────────────────────┘
              │ fail (up to 5 attempts, refresh between)
              ▼
   ┌────────────────────────┐
   │ 3. Human via noVNC      │  source≠"app" only; 200s timeout
   │    (wait_for_human)     │  → status=waiting_for_human
   └────────────────────────┘
```

The same idea generalizes via `HandoffNeeded`: deterministic by default, AI only where
reality is messy, human only as last resort.

---

## 6. Per-State Configuration

States differ in portal, permit, and payment — defined twice (API prompt-side in
`api/src/tasks/borderTax/states/`, worker script-side in
`worker/src/scripted/border_tax/params.py`):

```
 STATE   PAYMENT       TAX MODE   ENTRY DISTRICT   PATH
 ─────   ──────────    ────────   ──────────────   ──────────────────
 UP*     net banking   DAYS       GHAZIABAD        scripted + AI
 HR      net banking   DAYS       FARIDABAD        scripted + AI
 RJ      UPI (egras)   DAYS       CHITTORGARH      scripted (diff portal)
 PB      UPI           DAYS       MOHALI           scripted
 MP      UPI           DAYS       SHEOPUR          scripted
 UK      net banking   DAYS       DEHRADUN         scripted + web handover
 TN      UPI           WEEKLY     KRISHNAGIRI      scripted
 HP      net banking   DAYS       TIPRA            scripted, human pays
 BR      net banking   DAYS       PATNA            scripted, human pays
 (* = default state)
```

The API fills in any param the caller omits (`applyStateDefaults`), with alias resolution
(`"U.P."` → `"UTTAR PRADESH"`, etc.).

---

## 7. The Tasks System (API side)

Each task is a three-file module under `api/src/tasks/`:

| File        | Role                                                                  |
|-------------|-----------------------------------------------------------------------|
| `index.ts`  | Task definition: id, name, requiredParams, preprocessParams()         |
| `prompt.ts` | `buildPrompt(params, source)` — the full LLM instruction string       |
| `tool.ts`   | `TaskTool[]` — callable tools mapped to `/api/internal/*` endpoints    |

Registered tasks: `border-tax`, `challan-settlement`, `fetch-receipt`, `test-human`.

### Key API endpoints

| Endpoint                                  | Purpose                                  |
|-------------------------------------------|------------------------------------------|
| `POST /api/run`                           | Queue a job (dedupe + eligibility check) |
| `GET  /api/tasks`                         | List available task IDs                  |
| `GET  /api/jobs/:id/status`               | Poll job status                          |
| `GET  /api/jobs`                          | Paginated job listing                    |
| `POST /api/jobs/:id/intervene`            | Submit human input (OTP/CAPTCHA)         |
| `POST /api/jobs/:id/cancel`               | Cancel a queued/running job              |
| `GET  /api/dashboard`                     | HTML monitoring dashboard                |
| `POST /api/border-tax/check-eligibility`  | Pre-flight driver rate-limit check       |
| `POST /api/internal/challans/save`        | (worker→API) save extracted challans     |
| `POST /api/internal/discounts/save`       | (worker→API) save discount/settlement    |
| `POST /api/internal/border-tax/save-qr`   | (worker→API) upload UPI QR PNG → GCS      |
| `POST /api/internal/border-tax/save-receipt` | (worker→API) upload receipt PDF → GCS  |
| `POST /api/internal/job-completed`        | (worker→API) finalize job                |

### Eligibility / rate limits (`internal/borderTax/driverUsage.ts`)

- Non-members: max 1 lifetime border tax.
- Members: max 1/day, 30/month (resets on IST calendar boundaries).
- Blocks: `qr_generated` (45 min), `process_failed` (24 h), `paid_today` (until IST midnight).

---

## 8. Mental Model (one paragraph)

A client asks the **API** to "pay border tax for vehicle X in state Y." The API rate-limits
the driver, builds instructions, and drops a job on a **Redis** queue. A **worker** picks it
up, gives it a real Chromium browser on a virtual screen (watchable over VNC), and either runs
a fast **hand-coded script** or, where the site is unpredictable, an **AI agent** — escalating
CAPTCHAs from cheap OCR → AI → human. As it goes, it calls back to the API to **save the QR
code, the receipt PDF, and the final outcome** into Firebase. The client polls for status and
can feed in an OTP if asked. The whole thing is built to scale horizontally (many workers,
many slots) and to keep humans in the loop only when the robots genuinely can't proceed.

---

## Environment Variables Reference

| Variable                                 | Service | Default                  | Purpose                                  |
|------------------------------------------|---------|--------------------------|------------------------------------------|
| `REDIS_URL`                              | both    | `redis://localhost:6379` | Redis connection                         |
| `API_URL`                                | worker  | `http://api:3000`        | API backend for callbacks                |
| `DOMAIN`                                 | caddy   | `localhost`              | Public domain / VNC live-view URL        |
| `MAX_SLOTS`                              | worker  | `10`                     | Concurrent job limit                     |
| `SCRIPTED_BORDER_TAX_STATES`             | worker  | (empty)                  | States that bypass the LLM               |
| `SCRIPTED_BORDER_TAX_AUTO_CLEAR_PENDING` | worker  | `false`                  | Auto-clear pending-transaction popups    |
| `WEB_HANDOVER_POLL_QR`                   | worker  | `true`                   | Poll for QR during web handover          |
