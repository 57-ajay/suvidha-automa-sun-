import { FieldValue } from "firebase-admin/firestore";
import { db, challanRequestsRef, borderTaxRequestsRef } from "../firebase";

const agentConfigRef = db.collection("settings").doc("automationAgentConfig");

/**
 * Called (fire-and-forget) when a job completes.
 * - Finds the jobId inside the numbered slot maps (1, 2, 3…)
 * - Removes it from that slot's assignedRequestIds array
 * - If the array becomes empty, flips status --> "available"
 * - Decrements the top-level `count` by 1
 */
export async function releaseAgentSlot(jobId: string): Promise<{ ok: boolean; error?: string }> {
    if (!jobId) {
        console.log(`[agentConfig] skip: no jobId`);
        return { ok: false, error: "no jobId" };
    }

    try {
        const snap = await agentConfigRef.get();
        if (!snap.exists) {
            console.log(`[agentConfig] doc not found`);
            return { ok: false, error: "automationAgentConfig doc not found" };
        }

        const data = snap.data()!;
        let foundSlotKey: string | null = null;
        let slotData: any = null;

        // Iterate numbered slot keys (1, 2, 3, …)
        for (const key of Object.keys(data)) {
            if (!/^\d+$/.test(key)) continue; // skip non-numeric keys like "count"

            const slot = data[key];
            if (!slot || !Array.isArray(slot.assignedRequestIds)) continue;

            if (slot.assignedRequestIds.includes(jobId)) {
                foundSlotKey = key;
                slotData = slot;
                break;
            }
        }

        if (!foundSlotKey || !slotData) {
            console.log(`[agentConfig] jobId=${jobId} not found in any slot`);
            return { ok: false, error: "jobId not found in any slot" };
        }

        const updatedIds: string[] = slotData.assignedRequestIds.filter(
            (id: string) => id !== jobId
        );

        const updatePayload: Record<string, any> = {
            [`${foundSlotKey}.assignedRequestIds`]: updatedIds,
            count: FieldValue.increment(-1),
        };

        // If no more assigned requests, mark slot as available
        if (updatedIds.length === 0 && slotData.status === "busy") {
            updatePayload[`${foundSlotKey}.status`] = "available";
        }

        await agentConfigRef.update(updatePayload);

        console.log(
            `[agentConfig] released jobId=${jobId} from slot ${foundSlotKey} ` +
            `(remaining=${updatedIds.length}, statusFlip=${updatedIds.length === 0})`
        );

        return { ok: true };
    } catch (e) {
        console.error(`[agentConfig] ERROR releasing jobId=${jobId}:`, e);
        return { ok: false, error: (e as Error).message };
    }
}

/**
 * Save agent cost/usage data to the request document for the given task.
 *
 * Routes to the correct Firestore collection based on taskId:
 *   - "challan-settlement" → challanRequests/{requestId}
 *   - "border-tax"         → borderTaxRequests/{requestId}
 *
 * If taskId is omitted or unknown, falls back to challanRequests for
 * backwards compatibility — but logs a WARN so we can spot legacy callers.
 */
export async function saveAgentCost(
    requestId: string,
    jobId: string,
    costData: Record<string, any>,
    source?: string,
    taskId?: string,
): Promise<{ ok: boolean; error?: string }> {
    if (!requestId || !costData) {
        return { ok: false, error: "requestId and costData required" };
    }

    let docRef;
    let collectionLabel: string;
    if (taskId === "challan-settlement") {
        docRef = challanRequestsRef.doc(requestId);
        collectionLabel = "challanRequests";
    } else if (taskId === "border-tax") {
        docRef = borderTaxRequestsRef.doc(requestId);
        collectionLabel = "borderTaxRequests";
    } else {
        // Backwards compat: legacy callers don't pass taskId. Default to challan.
        console.log(
            `[saveAgentCost] WARN no/unknown taskId="${taskId}" — defaulting to challanRequests`
        );
        docRef = challanRequestsRef.doc(requestId);
        collectionLabel = "challanRequests (default)";
    }

    try {
        const docSnap = await docRef.get();
        if (!docSnap.exists) {
            console.log(
                `[saveAgentCost] ${collectionLabel} doc not found for requestId=${requestId}`
            );
            return { ok: false, error: `${collectionLabel} doc not found` };
        }

        await docRef.update({
            agentCost: {
                jobId,
                ...costData,
                source: source || "web",
                savedAt: FieldValue.serverTimestamp(),
            },
        });

        console.log(
            `[saveAgentCost] saved cost taskId=${taskId} collection=${collectionLabel} ` +
            `requestId=${requestId} jobId=${jobId} totalCost=${costData.totalCost} ` +
            `costSource=${costData.costSource ?? "n/a"}`
        );
        return { ok: true };
    } catch (e) {
        console.error(`[saveAgentCost] ERROR:`, e);
        return { ok: false, error: (e as Error).message };
    }
}
