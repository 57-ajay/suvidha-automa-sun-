import { FieldValue } from "firebase-admin/firestore";
import { db, challanRequestsRef } from "../../firebase";

export type ChallanAiStatus = "running" | "completed" | "failed";

/**
 * Set the per-challan `aiAgentStatus = { status, reason }` on the matching entry
 * in challanRequests/{requestId}.challans[] (falling back to challansDraft[]),
 * the same array entry the receipt URL is written to.
 *
 * Lifecycle: "running" when /api/run launches the job, then "completed" or
 * "failed" (with a reason) when the job finishes / is cancelled.
 *
 * Runs in a transaction: jobId == challanNo means multiple challans in the SAME
 * doc can run as concurrent jobs, and a plain read-modify-write of the whole
 * challans[] array would let concurrent writers clobber each other's entries.
 */
export async function setChallanAiAgentStatus(
    requestId: string | undefined,
    challanNo: string | undefined,
    status: ChallanAiStatus,
    reason: string = "",
): Promise<{ ok: boolean; error?: string }> {
    if (!requestId) return { ok: false, error: "requestId required" };
    if (!challanNo) return { ok: false, error: "challanNo required" };

    const docRef = challanRequestsRef.doc(requestId);
    const matches = (c: any) =>
        String(c?.challanNo) === String(challanNo) || String(c?.id) === String(challanNo);
    const aiAgentStatus = { status, reason: reason ?? "" };

    try {
        const attachedTo = await db.runTransaction(async (tx) => {
            const snap = await tx.get(docRef);
            if (!snap.exists) {
                throw new Error(`No challanRequest found for requestId ${requestId}`);
            }
            const data = snap.data()!;
            const challans: any[] = Array.isArray(data.challans) ? data.challans : [];
            const draft: any[] = Array.isArray(data.challansDraft) ? data.challansDraft : [];

            if (challans.some(matches)) {
                tx.update(docRef, {
                    challans: challans.map((c) => (matches(c) ? { ...c, aiAgentStatus } : c)),
                    updatedAt: FieldValue.serverTimestamp(),
                });
                return "challans";
            }
            if (draft.some(matches)) {
                tx.update(docRef, {
                    challansDraft: draft.map((c) => (matches(c) ? { ...c, aiAgentStatus } : c)),
                    updatedAt: FieldValue.serverTimestamp(),
                });
                return "challansDraft";
            }
            throw new Error(
                `challanNo ${challanNo} not found in challans/challansDraft for requestId ${requestId}`,
            );
        });

        console.log(
            `[challanAiStatus] set "${status}" on ${attachedTo}[challanNo=${challanNo}] ` +
            `in challanRequests/${requestId}`,
        );
        return { ok: true };
    } catch (e) {
        console.error(
            `[challanAiStatus] failed requestId=${requestId} challanNo=${challanNo} status=${status}:`,
            e,
        );
        return { ok: false, error: (e as Error).message };
    }
}
