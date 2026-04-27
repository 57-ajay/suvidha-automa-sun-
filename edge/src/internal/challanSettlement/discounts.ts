import { Timestamp, FieldValue } from "firebase-admin/firestore";
import { db, challanRequestsRef } from "../../firebase";

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

    // Coerce amounts from strings to numbers and validate
    const incoming: AgentDiscount[] = [];
    for (const d of rawIncoming) {
        if (!d.challanId || typeof d.challanId !== "string") {
            console.log(`[save_discounts] FAIL: invalid challanId in ${JSON.stringify(d)}`);
            return { ok: false, error: `Invalid challanId: ${JSON.stringify(d)}` };
        }

        const discountAmount = toNumber(d.discountAmount);
        const originalAmount = toNumber(d.originalAmount);

        if (discountAmount === null) {
            console.log(`[save_discounts] FAIL: cannot parse discountAmount for ${d.challanId}: ${d.discountAmount} (${typeof d.discountAmount})`);
            return { ok: false, error: `Invalid discountAmount for challan ${d.challanId}: ${d.discountAmount}` };
        }
        if (originalAmount === null) {
            console.log(`[save_discounts] FAIL: cannot parse originalAmount for ${d.challanId}: ${d.originalAmount} (${typeof d.originalAmount})`);
            return { ok: false, error: `Invalid originalAmount for challan ${d.challanId}: ${d.originalAmount}` };
        }

        incoming.push({
            challanId: d.challanId.trim(),
            discountAmount,
            originalAmount,
        });
    }

    console.log(`[save_discounts] incoming count=${incoming.length}`);
    for (const d of incoming) {
        console.log(`[save_discounts]   incoming: challanId="${d.challanId}" discount=${d.discountAmount} original=${d.originalAmount}`);
    }

    // Get the challanRequest doc directly by requestId
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
        // -----------------------------------------------------------------
        // No challans exist yet (save_challans was not called or found none).
        // Create challan entries directly from the discount data.
        // -----------------------------------------------------------------
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
        // -----------------------------------------------------------------
        // Challans exist — match and merge discounts
        // -----------------------------------------------------------------
        const existingIds = existingChallans.map((c: any) => c.id);
        const incomingIds = incoming.map(d => d.challanId);
        console.log(`[save_discounts] existing IDs: ${JSON.stringify(existingIds)}`);
        console.log(`[save_discounts] incoming IDs: ${JSON.stringify(incomingIds)}`);

        // Try to match existing challans with discounts
        updatedChallans = existingChallans.map((challan: any) => {
            const discount = discountMap.get(challan.id);
            if (discount && discount.discountAmount != null) {
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

        // Add any incoming discounts that didn't match an existing challan
        // (Virtual Courts may have challans that Delhi Traffic Police didn't)
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
                    // isSelected: true,
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

    // Write each challan to subChallans sub-collection
    const subChallansRef = db.collection(`challans/${vehicleNumber}/subChallans`);

    const subDocPromises = updatedChallans.map((challan: any) => {
        const subDoc = {
            challanAmount: challan.challanAmount ?? null,
            challanDate: challan.challanDate ?? null,
            challanNo: challan.challanNo ?? null,
            id: challan.id,
            location: challan.location ?? null,
            offence: challan.offence ?? null,
            paymentDetails: challan.paymentDetails ?? null,
            quotation: challan.quotation ?? null,
            status: challan.status || "unpaid",
            settlementStatus: challan.status || "unpaid",
            type: challan.type ?? null,
        };
        return subChallansRef.doc(challan.id).set(subDoc, { merge: true });
    });

    await Promise.all(subDocPromises);

    // Update main request doc
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
        `[save_discounts] SUCCESS job=${jobId} vehicle=${vehicleNumber} matched=${matched} created=${created} total=₹${totalSettlementAmount} doc=${docSnap.id}`
    );

    return {
        ok: true,
        matched,
        created,
        total: incoming.length,
        totalSettlementAmount,
        vehicle: vehicleNumber,
        docId: docSnap.id,
    };
}
