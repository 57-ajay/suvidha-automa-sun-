import { FieldValue } from "firebase-admin/firestore";
import { db } from "../firebase";

const agentWorkSummaryRef = db.collection("agentWorkSummary");

export interface SaveAgentWorkSummaryInput {
    jobId: string;
    requestId: string;
    taskId?: string;
    vehicleNumber?: string;
    status: "done" | "failed" | "partial";
    summary?: string;
    error?: string;
    source?: string;
    params?: Record<string, any>;
    costData?: Record<string, any>;
    partialReasons?: string[];
}

/**
 * Saves the agent's final summary under `agentWorkSummary/{requestId}`.
 * Uses merge:true so a later failure/retry overwrites fields without
 * losing earlier ones.
 *
 * Called fire-and-forget from the /api/internal/job-completed handler.
 */
export async function saveAgentWorkSummary(
    input: SaveAgentWorkSummaryInput
): Promise<{ ok: boolean; error?: string }> {
    const { requestId } = input;
    if (!requestId) {
        console.log(`[agentWorkSummary] skip: no requestId`);
        return { ok: false, error: "requestId required" };
    }

    try {
        const payload: Record<string, any> = {
            requestId,
            jobId: input.jobId,
            status: input.status,
            source: input.source ?? "web",
            completedAt: FieldValue.serverTimestamp(),
        };

        if (input.taskId) payload.taskId = input.taskId;
        if (input.vehicleNumber) payload.vehicleNumber = input.vehicleNumber;
        if (input.summary) payload.summary = input.summary;
        if (input.error) payload.error = input.error;
        if (input.params) payload.params = input.params;
        if (input.costData) payload.cost = input.costData;
        if (input.partialReasons && input.partialReasons.length > 0) {
            payload.partialReasons = input.partialReasons;
        }

        await agentWorkSummaryRef.doc(requestId).set(payload, { merge: true });

        console.log(
            `[agentWorkSummary] saved requestId=${requestId} jobId=${input.jobId} status=${input.status} source=${input.source}`
        );
        return { ok: true };
    } catch (e) {
        console.error(`[agentWorkSummary] ERROR saving requestId=${requestId}:`, e);
        return { ok: false, error: (e as Error).message };
    }
}
