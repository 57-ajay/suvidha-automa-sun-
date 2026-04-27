import { FieldValue } from "firebase-admin/firestore";
import { challanRequestsRef, borderTaxRequestsRef } from "../firebase";

export async function setAssignedPartner(
    requestId: string,
    taskId: string,
): Promise<{ ok: boolean; error?: string }> {
    if (!requestId) {
        return { ok: false, error: "requestId required" };
    }

    let docRef;
    if (taskId === "challan-settlement") {
        docRef = challanRequestsRef.doc(requestId);
    } else if (taskId === "border-tax") {
        docRef = borderTaxRequestsRef.doc(requestId);
    } else {
        console.log(`[assignedPartner] skip: taskId="${taskId}" not supported`);
        return { ok: false, error: `taskId ${taskId} not supported` };
    }

    try {
        await docRef.update({
            assignedPartner: {
                at: FieldValue.serverTimestamp(),
                name: "AI Agent",
                id: "ai_agent",
                withAiAgent: true,
            },
        });

        console.log(
            `[assignedPartner] set AI Agent for taskId=${taskId} requestId=${requestId}`,
        );
        return { ok: true };
    } catch (e) {
        console.error(
            `[assignedPartner] ERROR taskId=${taskId} requestId=${requestId}:`,
            e,
        );
        return { ok: false, error: (e as Error).message };
    }
}
