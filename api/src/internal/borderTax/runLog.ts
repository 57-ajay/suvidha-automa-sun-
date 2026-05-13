// api/src/internal/borderTax/runLog.ts
//
// Persist the automation log from the worker as ONE consolidated Firestore
// doc per job run.
//
// Storage shape:
//
//   borderTaxRequests/{requestId}/agentAutomationLog/{jobId}
//     {
//       jobId, requestId,
//       finalStatus, abortReason, summary,
//       totalCostUsd, stepCount, handoffCount,
//       captchaAttempts, errorCount, durationMs,
//       partialReasons,
//       persistedAt,
//       steps: [ ...StepLog ]
//     }
//
// One doc = one full run. Multiple jobIds per requestId (retries) live as
// separate sibling docs in the same subcollection. Doc size cap (1 MiB) is
// fine -- a 50-step run is ~25 KB.
//
// Denormalized top-level fields exist so you can run useful Firestore
// queries without pulling the steps array. Examples:
//   - all failed scripted runs:        where('finalStatus', '==', 'failed')
//   - runs that needed AI rescue:      where('handoffCount', '>', 0)
//   - runs that hit captcha problems:  where('captchaAttempts', '>', 1)

import { FieldValue } from "firebase-admin/firestore";
import { borderTaxRequestsRef } from "../../firebase";


export interface RunLogStep {
    index: number;
    name: string;
    status: string;
    url?: string | null;
    selector?: string | null;
    value?: string | null;
    attempt?: number;
    duration_ms?: number;
    error?: string | null;
    handoff_reason?: string | null;
    handoff_summary?: string | null;
    handoff_cost_usd?: number | null;
    started_at?: string;
}


export interface SaveRunLogInput {
    jobId: string;
    requestId: string;
    steps: RunLogStep[];
    // Additional context from the job-completed payload. All optional --
    // the handler derives what it can from `steps` if these are missing.
    finalStatus?: string;             // "done" | "partial" | "failed"
    abortReason?: string;             // populated on failure
    summary?: string;                 // human-readable run summary
    totalCostUsd?: number;            // from costData.totalCost
    partialReasons?: string[];        // when finalStatus === "partial"
}


export interface SaveRunLogResult {
    ok: boolean;
    error?: string;
    docPath?: string;
}


function deriveSummary(steps: RunLogStep[]) {
    let durationMs = 0;
    let handoffCount = 0;
    let errorCount = 0;
    let captchaAttempts = 0;

    for (const s of steps) {
        durationMs += s.duration_ms || 0;
        if (s.handoff_reason) handoffCount++;
        if (s.status === "failed") errorCount++;
        if (
            s.handoff_reason
            && s.handoff_reason.startsWith("captcha")
            && (s.attempt || 1) > captchaAttempts
        ) {
            captchaAttempts = s.attempt || 1;
        }
    }

    return { durationMs, handoffCount, errorCount, captchaAttempts };
}


export async function handleSaveRunLog(
    input: SaveRunLogInput,
): Promise<SaveRunLogResult> {
    const {
        jobId, requestId, steps,
        finalStatus, abortReason, summary,
        totalCostUsd, partialReasons,
    } = input;

    if (!requestId) {
        return { ok: false, error: "requestId is required" };
    }
    if (!jobId) {
        return { ok: false, error: "jobId is required" };
    }
    if (!Array.isArray(steps) || steps.length === 0) {
        // AI-path jobs (and any other no-runlog jobs) just skip.
        return { ok: true };
    }

    const derived = deriveSummary(steps);

    const doc = {
        jobId,
        requestId,
        finalStatus: finalStatus ?? null,
        abortReason: abortReason ?? null,
        summary: summary ?? null,
        totalCostUsd: typeof totalCostUsd === "number" ? totalCostUsd : 0,
        stepCount: steps.length,
        handoffCount: derived.handoffCount,
        captchaAttempts: derived.captchaAttempts || null,
        errorCount: derived.errorCount,
        durationMs: derived.durationMs,
        partialReasons: partialReasons && partialReasons.length > 0
            ? partialReasons
            : null,
        persistedAt: FieldValue.serverTimestamp(),
        steps,
    };

    const docPath =
        `borderTaxRequests/${requestId}/agentAutomationLog/${jobId}`;

    console.log(
        `[runLog] writing ${steps.length}-step run for `
        + `requestId=${requestId} jobId=${jobId} `
        + `finalStatus=${finalStatus ?? "?"} `
        + `handoffs=${derived.handoffCount} errors=${derived.errorCount}`,
    );

    try {
        const ref = borderTaxRequestsRef
            .doc(requestId)
            .collection("agentAutomationLog")
            .doc(jobId);
        await ref.set(doc);
        return { ok: true, docPath };
    } catch (e: any) {
        const msg = `runLog write failed: ${e.message}`;
        console.error(`[runLog]   ERROR: ${msg}`);
        return { ok: false, error: msg };
    }
}
