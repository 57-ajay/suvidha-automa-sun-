import { Timestamp, FieldValue } from "firebase-admin/firestore";
import { challanRequestsRef } from "../../firebase";
import { priceForOffence } from "../../tasks/challanSettlement/prompt";

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

    const incoming: AgentChallan[] = [];
    const droppedReasons: { challanId: string; reason: string; offence?: string }[] = [];
    const filledFromKeyword: { challanId: string; offence: string; price: number }[] = [];

    for (const c of rawIncoming) {
        if (!c || !c.challanId || typeof c.challanId !== "string") {
            console.warn(`[save_challans] DROP invalid_challanId: ${JSON.stringify(c)}`);
            droppedReasons.push({ challanId: String(c?.challanId ?? ""), reason: "invalid_challanId" });
            continue;
        }

        const offence = (c.offence ?? "").toString();
        const parsedAmount = toNumber(c.amount);

        let amount: number;
        if (parsedAmount === null || parsedAmount === 0) {
            const derived = priceForOffence(offence);
            if (derived !== null) {
                console.warn(
                    `[save_challans] FILL_FROM_KEYWORD ${c.challanId}: ` +
                    `amount=${c.amount} (${typeof c.amount}) offence="${offence}" → price=${derived}`
                );
                amount = derived;
                filledFromKeyword.push({ challanId: c.challanId, offence, price: derived });
            } else {
                console.warn(
                    `[save_challans] DROP zero_or_missing_amount ${c.challanId}: ` +
                    `amount=${c.amount} offence="${offence}" — no keyword match for fallback price`
                );
                droppedReasons.push({ challanId: c.challanId, reason: "zero_or_missing_amount_no_keyword", offence });
                continue;
            }
        } else {
            amount = parsedAmount;
        }

        if (amount < 0) {
            console.warn(`[save_challans] DROP negative_amount ${c.challanId}: amount=${amount}`);
            droppedReasons.push({ challanId: c.challanId, reason: "negative_amount" });
            continue;
        }

        incoming.push({
            challanId: c.challanId.trim(),
            offence,
            amount,
            date: (c.date ?? "").toString(),
        });
    }

    console.log(`[save_challans] after-validation count=${incoming.length} dropped=${droppedReasons.length} filled=${filledFromKeyword.length}`);
    if (droppedReasons.length > 0) {
        console.warn(`[save_challans] dropped records: ${JSON.stringify(droppedReasons)}`);
    }
    if (filledFromKeyword.length > 0) {
        console.warn(`[save_challans] filled from keyword: ${JSON.stringify(filledFromKeyword)}`);
    }

    if (incoming.length === 0) {
        console.warn(`[save_challans] all_records_dropped vehicle=${vehicleNumber} job=${jobId}`);
        return {
            ok: true,
            saved: 0,
            total: rawIncoming.length,
            dropped: droppedReasons.length,
            droppedReasons,
            note: "All records were dropped during validation. See droppedReasons. No save was performed.",
            vehicle: vehicleNumber,
        };
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

    const existingMap = new Map<string, any>();
    for (const c of existingChallans) {
        if (c.id) {
            existingMap.set(c.id, c);
        }
    }

    const mergedChallans = incoming.map((c) => {
        const existing = existingMap.get(c.challanId);
        const merged = {
            challanAmount: c.amount,
            challanDate: parseDate(c.date),
            challanNo: c.challanId,
            id: c.challanId,
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
    console.log(
        `[save_challans] SUCCESS job=${jobId} vehicle=${vehicleNumber} ` +
        `saved=${mergedChallans.length} dropped=${droppedReasons.length} filled=${filledFromKeyword.length} ` +
        `doc=${docSnap.id}`
    );
    console.log(`[save_challans] saved IDs: ${JSON.stringify(savedIds)}`);

    return {
        ok: true,
        saved: mergedChallans.length,
        dropped: droppedReasons.length,
        droppedReasons: droppedReasons.length > 0 ? droppedReasons : undefined,
        filledFromKeyword: filledFromKeyword.length > 0 ? filledFromKeyword : undefined,
        vehicle: vehicleNumber,
        docId: docSnap.id,
    };
}
