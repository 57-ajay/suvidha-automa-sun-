import { Timestamp, FieldValue } from "firebase-admin/firestore";
import { challanRequestsRef } from "../../firebase";

interface AgentDiscount {
    challanId: string;
    discountAmount: number;
    originalAmount: number;
}

interface InternalRequest {
    jobId: string;
    params: Record<string, string>;
    data: unknown;
}

/** Coerce a value to a number — handles strings like "2000" from LLM output */
function toNumber(val: unknown): number | null {
    if (typeof val === "number") return val;
    if (typeof val === "string") {
        const n = Number(val);
        return isNaN(n) ? null : n;
    }
    return null;
}

export async function handleSaveDiscounts(body: InternalRequest) {
    const jobId = body.jobId ?? "unknown";

    console.log(`[save_discounts] START job=${jobId}`);
    console.log(`[save_discounts] params=${JSON.stringify(body.params)}`);
    console.log(`[save_discounts] data type=${typeof body.data}, isArray=${Array.isArray(body.data)}`);
    console.log(`[save_discounts] raw data=${JSON.stringify(body.data)?.substring(0, 1000)}`);

    const vehicleNumber = body.params?.vehicleNumber;
    if (!vehicleNumber) {
        console.log(`[save_discounts] FAIL: vehicleNumber missing`);
        return { ok: false, error: "vehicleNumber missing from job params" };
    }

    const requestId = body.params?.requestId;
    if (!requestId) {
        console.log(`[save_discounts] FAIL: requestId missing`);
        return { ok: false, error: "requestId missing from job params" };
    }

    let rawIncoming: any[];

    if (Array.isArray(body.data)) {
        rawIncoming = body.data;
    } else if (typeof body.data === "string") {
        console.log(`[save_discounts] WARN: data is string, attempting JSON parse`);
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
        return { ok: false, error: "data must be a non-empty array of discounts" };
    }

    const incoming: AgentDiscount[] = [];
    const droppedReasons: { challanId: string; reason: string }[] = [];

    for (const d of rawIncoming) {
        if (!d || !d.challanId || typeof d.challanId !== "string") {
            console.warn(`[save_discounts] DROP invalid_challanId: ${JSON.stringify(d)}`);
            droppedReasons.push({ challanId: String(d?.challanId ?? ""), reason: "invalid_challanId" });
            continue;
        }

        const discountAmount = toNumber(d.discountAmount);
        const originalAmount = toNumber(d.originalAmount);

        if (discountAmount === null) {
            console.warn(`[save_discounts] DROP unparseable_discount ${d.challanId}: ${d.discountAmount} (${typeof d.discountAmount})`);
            droppedReasons.push({ challanId: d.challanId, reason: "unparseable_discount" });
            continue;
        }
        if (originalAmount === null) {
            console.warn(`[save_discounts] DROP unparseable_original ${d.challanId}: ${d.originalAmount} (${typeof d.originalAmount})`);
            droppedReasons.push({ challanId: d.challanId, reason: "unparseable_original" });
            continue;
        }

        if (discountAmount > originalAmount) {
            console.warn(
                `[save_discounts] DROP exceeds_original ${d.challanId}: ` +
                `discount=${discountAmount} > original=${originalAmount} ` +
                `vehicle=${vehicleNumber} requestId=${requestId} jobId=${jobId}`
            );
            droppedReasons.push({ challanId: d.challanId, reason: "discount_exceeds_original" });
            continue;
        }

        // Negative amounts are nonsense.
        if (discountAmount < 0 || originalAmount < 0) {
            console.warn(`[save_discounts] DROP negative_amount ${d.challanId}: discount=${discountAmount} original=${originalAmount}`);
            droppedReasons.push({ challanId: d.challanId, reason: "negative_amount" });
            continue;
        }

        // originalAmount of 0 doesn't make sense (every challan has some fine).
        if (originalAmount === 0) {
            console.warn(`[save_discounts] DROP zero_original ${d.challanId}: original=0`);
            droppedReasons.push({ challanId: d.challanId, reason: "zero_original" });
            continue;
        }

        incoming.push({
            challanId: d.challanId.trim(),
            discountAmount,
            originalAmount,
        });
    }

    console.log(`[save_discounts] after-validation count=${incoming.length} dropped=${droppedReasons.length}`);
    if (droppedReasons.length > 0) {
        console.warn(`[save_discounts] dropped records: ${JSON.stringify(droppedReasons)}`);
    }

    for (const d of incoming) {
        console.log(`[save_discounts]   accepted: challanId="${d.challanId}" discount=${d.discountAmount} original=${d.originalAmount}`);
        if (d.discountAmount === 0 && d.originalAmount > 0) {
            console.warn(
                `[save_discounts] SUSPICIOUS_ZERO: challanId=${d.challanId} ` +
                `discount=0 with original=${d.originalAmount} ` +
                `vehicle=${vehicleNumber} requestId=${requestId} jobId=${jobId}. ` +
                `Saving as instructed — verify on Virtual Courts that "Proposed Fine" really shows 0.`
            );
        }
    }

    if (incoming.length === 0) {
        console.warn(`[save_discounts] all_records_dropped vehicle=${vehicleNumber} job=${jobId}`);
        return {
            ok: true,
            saved: 0,
            matched: 0,
            created: 0,
            total: rawIncoming.length,
            dropped: droppedReasons.length,
            droppedReasons,
            note: "All records were dropped during validation. See droppedReasons for details. No save was performed.",
            vehicle: vehicleNumber,
        };
    }

    const docRef = challanRequestsRef.doc(requestId);
    const docSnap = await docRef.get();

    if (!docSnap.exists) {
        console.log(`[save_discounts] FAIL: no challanRequest doc for requestId=${requestId}`);
        return { ok: false, error: `No challanRequest found for requestId ${requestId}` };
    }

    const docData = docSnap.data()!;
    const existingChallans: any[] = docData.challansDraft || [];

    console.log(`[save_discounts] doc=${docSnap.id} existing challans=${existingChallans.length}`);

    const now = new Date();
    const discountMap = new Map<string, AgentDiscount>();
    for (const d of incoming) {
        discountMap.set(d.challanId, d);
    }

    let totalSettlementAmount = 0;
    let matched = 0;
    let created = 0;
    let updatedChallans: any[];

    if (existingChallans.length === 0) {
        console.log(`[save_discounts] No existing challans — creating from discount data`);

        updatedChallans = incoming.map((d) => {
            totalSettlementAmount += d.discountAmount;
            created++;
            return {
                challanAmount: d.originalAmount,
                challanDate: Timestamp.fromDate(now),
                challanNo: d.challanId,
                id: d.challanId,
                isSelected: true,
                offence: null,
                quotation: {
                    amount: d.discountAmount,
                    at: Timestamp.fromDate(now),
                    settlementAmountAdded: true,
                },
            };
        });

        console.log(`[save_discounts] created ${created} challan entries from discount data`);
    } else {
        const existingIds = existingChallans.map((c: any) => c.id);
        const incomingIds = incoming.map(d => d.challanId);
        console.log(`[save_discounts] existing IDs: ${JSON.stringify(existingIds)}`);
        console.log(`[save_discounts] incoming IDs: ${JSON.stringify(incomingIds)}`);

        updatedChallans = existingChallans.map((challan: any) => {
            const discount = discountMap.get(challan.id);
            if (discount && discount.discountAmount != null) {
                const existingChallanAmount = toNumber(challan.challanAmount);
                if (existingChallanAmount !== null && existingChallanAmount > 0 && discount.discountAmount > existingChallanAmount) {
                    console.warn(
                        `[save_discounts] DROP-AT-MERGE ${challan.id}: ` +
                        `discount=${discount.discountAmount} > existing challanAmount=${existingChallanAmount}. ` +
                        `Keeping existing quotation untouched.`
                    );
                    droppedReasons.push({ challanId: challan.id, reason: "discount_exceeds_existing_amount" });
                    if (challan.quotation?.amount != null) {
                        totalSettlementAmount += challan.quotation.amount;
                    }
                    return challan;
                }

                matched++;
                totalSettlementAmount += discount.discountAmount;
                console.log(`[save_discounts]   MATCHED ${challan.id} → discount=₹${discount.discountAmount}`);
                return {
                    ...challan,
                    quotation: {
                        amount: discount.discountAmount,
                        at: Timestamp.fromDate(now),
                        settlementAmountAdded: true,
                    },
                };
            }
            if (challan.quotation?.amount != null) {
                totalSettlementAmount += challan.quotation.amount;
            }
            return challan;
        });

        for (const d of incoming) {
            if (!existingChallans.some((c: any) => c.id === d.challanId)) {
                created++;
                totalSettlementAmount += d.discountAmount;
                console.log(`[save_discounts]   NEW (unmatched) ${d.challanId} → discount=₹${d.discountAmount}`);
                updatedChallans.push({
                    challanAmount: d.originalAmount,
                    challanDate: Timestamp.fromDate(now),
                    challanNo: d.challanId,
                    id: d.challanId,
                    offence: null,
                    quotation: {
                        amount: d.discountAmount,
                        at: Timestamp.fromDate(now),
                        settlementAmountAdded: true,
                    },
                });
            }
        }

        const matchingIds = existingIds.filter((id: string) => discountMap.has(id));
        console.log(`[save_discounts] matched=${matched} created=${created} (${matchingIds.length} ID overlaps)`);
    }

    await docRef.update({
        challansDraft: updatedChallans,
        challansUpdatedBy: "agent",
        totalSettlementAmount,
        updatedAt: FieldValue.serverTimestamp(),
        paymentValidTill: Timestamp.fromDate(
            new Date(now.getTime() + 24 * 60 * 60 * 1000)
        ),
    });

    console.log(
        `[save_discounts] SUCCESS job=${jobId} vehicle=${vehicleNumber} ` +
        `matched=${matched} created=${created} dropped=${droppedReasons.length} ` +
        `total=₹${totalSettlementAmount} doc=${docSnap.id}`
    );

    return {
        ok: true,
        matched,
        created,
        total: incoming.length,
        dropped: droppedReasons.length,
        droppedReasons: droppedReasons.length > 0 ? droppedReasons : undefined,
        totalSettlementAmount,
        vehicle: vehicleNumber,
        docId: docSnap.id,
    };
}
