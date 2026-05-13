import { FieldValue } from "firebase-admin/firestore";
import { challanRequestsRef, borderTaxRequestsRef } from "../firebase";

export type AiAgentWorkStatus = "started" | "completed" | "partial" | "failed";

export async function setAiAgentWorkStatus(
    requestId: string | undefined,
    taskId: string | undefined,
    status: AiAgentWorkStatus,
): Promise<{ ok: boolean; error?: string }> {
    if (!requestId) {
        return { ok: false, error: "requestId required" };
    }
    if (!taskId) {
        return { ok: false, error: "taskId required" };
    }

    let docRef;
    if (taskId === "challan-settlement") {
        docRef = challanRequestsRef.doc(requestId);
    } else if (taskId === "border-tax") {
        docRef = borderTaxRequestsRef.doc(requestId);
    } else {
        console.log(
            `[aiAgentWorkStatus] skip: taskId="${taskId}" has no request doc (status=${status})`,
        );
        return { ok: false, error: `taskId ${taskId} not supported` };
    }

    try {
        await docRef.update({
            aiAgentWorkStatus: status,
            aiAgentWorkStatusUpdatedAt: FieldValue.serverTimestamp(),
        });
        console.log(
            `[aiAgentWorkStatus] set "${status}" for taskId=${taskId} requestId=${requestId}`,
        );
        return { ok: true };
    } catch (e) {
        console.error(
            `[aiAgentWorkStatus] ERROR taskId=${taskId} requestId=${requestId} status=${status}:`,
            e,
        );
        return { ok: false, error: (e as Error).message };
    }
}
