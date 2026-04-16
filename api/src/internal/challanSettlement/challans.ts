import { Timestamp, FieldValue } from "firebase-admin/firestore";
import { challanRequestsRef } from "../../firebase";

export interface AgentChallan {
    challanId: string;
    offence: string;
    amount: number;
    date: string;
}

export interface InternalRequest {
    jobId: string;
    params: Record<string, string>;
    data: unknown;
}

/** Coerce a value to a number — handles strings like "500" from LLM output */
function toNumber(val: unknown): number | null {
    if (typeof val === "number") return val;
    if (typeof val === "string") {
        const n = Number(val);
        return isNaN(n) ? null : n;
    }
    return null;
}

function parseDate(dateStr: string): Timestamp {
    const parsed = new Date(dateStr);
    if (isNaN(parsed.getTime())) {
        return Timestamp.now();
    }
    return Timestamp.fromDate(parsed);
}

export async function handleSaveChallans(body: InternalRequest) {
    const jobId = body.jobId ?? "unknown";

    console.log(`[save_challans] START job=${jobId}`);
    console.log(`[save_challans] params=${JSON.stringify(body.params)}`);
    console.log(`[save_challans] data type=${typeof body.data}, isArray=${Array.isArray(body.data)}`);
    console.log(`[save_challans] raw data=${JSON.stringify(body.data)?.substring(0, 1000)}`);

    const vehicleNumber = body.params?.vehicleNumber;
    if (!vehicleNumber) {
        console.log(`[save_challans] FAIL: vehicleNumber missing`);
        return { ok: false, error: "vehicleNumber missing from job params" };
    }

    const requestId = body.params?.requestId;
    if (!requestId) {
        console.log(`[save_challans] FAIL: requestId missing`);
        return { ok: false, error: "requestId missing from job params" };
    }

    // Robust data parsing
    let rawIncoming: any[];

    if (Array.isArray(body.data)) {
        rawIncoming = body.data;
    } else if (typeof body.data === "string") {
        console.log(`[save_challans] WARN: data is string, attempting JSON parse`);
        try {
            const parsed = JSON.parse(body.data);
            if (Array.isArray(parsed)) {
                rawIncoming = parsed;
            } else if (parsed && typeof parsed === "object") {
                rawIncoming = [parsed];
            } else {
                return { ok: false, error: "data string did not parse to array" };
            }
        } catch (e) {
            return { ok: false, error: `data is a string but not valid JSON: ${(e as Error).message}` };
        }
    } else {
        return { ok: false, error: `data must be an array, got ${typeof body.data}` };
    }

    if (rawIncoming.length === 0) {
        return { ok: false, error: "data must be a non-empty array of challans" };
    }

    // Coerce and validate
    const incoming: AgentChallan[] = [];
    for (const c of rawIncoming) {
        if (!c.challanId || typeof c.challanId !== "string") {
            console.log(`[save_challans] FAIL: invalid challanId in ${JSON.stringify(c)}`);
            return { ok: false, error: `Invalid challanId: ${JSON.stringify(c)}` };
        }

        const amount = toNumber(c.amount);
        if (amount === null || amount <= 0) {
            console.log(`[save_challans] FAIL: invalid amount for ${c.challanId}: ${c.amount} (${typeof c.amount})`);
            return { ok: false, error: `Invalid amount for challan ${c.challanId}: ${c.amount}` };
        }

        incoming.push({
            challanId: c.challanId.trim(),
            offence: c.offence || "",
            amount,
            date: c.date || "",
        });
    }

    console.log(`[save_challans] incoming count=${incoming.length}`);
    for (const c of incoming) {
        console.log(`[save_challans]   incoming: challanId="${c.challanId}" offence="${c.offence}" amount=${c.amount} date=${c.date}`);
    }

    const docRef = challanRequestsRef.doc(requestId);
    const docSnap = await docRef.get();

    if (!docSnap.exists) {
        console.log(`[save_challans] FAIL: no challanRequest doc for requestId=${requestId}`);
        return { ok: false, error: `No challanRequest found for requestId ${requestId}` };
    }

    const docData = docSnap.data()!;
    const existingChallans: any[] = docData.challansDraft || [];

    console.log(`[save_challans] doc=${docSnap.id} existing challans=${existingChallans.length}`);

    // Build a map of existing challans by id to preserve quotation data
    const existingMap = new Map<string, any>();
    for (const c of existingChallans) {
        if (c.id) {
            existingMap.set(c.id, c);
        }
    }

    // Merge: update existing challans with fresh data, add new ones
    const mergedChallans = incoming.map((c) => {
        const existing = existingMap.get(c.challanId);
        const merged = {
            challanAmount: c.amount,
            challanDate: parseDate(c.date),
            challanNo: c.challanId,
            id: c.challanId,
            // isSelected: true,
            offence: c.offence || null,
            ...(existing?.quotation ? { quotation: existing.quotation } : {}),
        };
        console.log(`[save_challans]   saving: id="${merged.id}" amount=${merged.challanAmount}`);
        return merged;
    });

    await docRef.update({
        challansDraft: mergedChallans,
        updatedAt: FieldValue.serverTimestamp(),
    });

    const savedIds = mergedChallans.map(c => c.id);
    console.log(`[save_challans] SUCCESS job=${jobId} vehicle=${vehicleNumber} saved=${mergedChallans.length} doc=${docSnap.id}`);
    console.log(`[save_challans] saved IDs: ${JSON.stringify(savedIds)}`);

    return {
        ok: true,
        saved: mergedChallans.length,
        vehicle: vehicleNumber,
        docId: docSnap.id,
    };
}
