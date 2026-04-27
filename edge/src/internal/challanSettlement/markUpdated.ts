import { challanRequestsRef } from "../../firebase";

export async function markChallansUpdatedByAgent(
    requestId: string,
): Promise<{ ok: boolean; error?: string }> {
    if (!requestId) {
        return { ok: false, error: "requestId required" };
    }

    try {
        await challanRequestsRef.doc(requestId).update({
            challansUpdatedBy: "agent",
        });
        console.log(
            `[markChallansUpdatedByAgent] set for requestId=${requestId}`,
        );
        return { ok: true };
    } catch (e) {
        console.error(
            `[markChallansUpdatedByAgent] ERROR requestId=${requestId}:`,
            e,
        );
        return { ok: false, error: (e as Error).message };
    }
}
