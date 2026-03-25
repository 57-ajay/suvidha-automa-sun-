import { Timestamp, FieldValue } from "firebase-admin/firestore";
import { db, challanRequestsRef } from "../firebase";

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

export async function handleSaveDiscounts(body: InternalRequest) {
    const vehicleNumber = body.params?.vehicleNumber;
    if (!vehicleNumber) {
        return { ok: false, error: "vehicleNumber missing from job params" };
    }

    const incoming = body.data as AgentDiscount[];
    if (!Array.isArray(incoming) || incoming.length === 0) {
        return { ok: false, error: "data must be a non-empty array of discounts" };
    }

    // Validate
    for (const d of incoming) {
        if (!d.challanId || typeof d.challanId !== "string") {
            return { ok: false, error: `Invalid challanId: ${JSON.stringify(d)}` };
        }
        if (typeof d.discountAmount !== "number") {
            return { ok: false, error: `Invalid discountAmount for challan ${d.challanId}` };
        }
    }

    // Find the challanRequest doc
    const snapshot = await challanRequestsRef
        .where("vehicleDetails.regNo", "==", vehicleNumber)
        .limit(1)
        .get();

    if (snapshot.empty) {
        return { ok: false, error: `No challanRequest found for vehicle ${vehicleNumber}` };
    }

    const docRef = snapshot.docs[0]!.ref;
    const docData = snapshot.docs[0]!.data();
    const existingChallans: any[] = docData.challans || [];

    if (existingChallans.length === 0) {
        return { ok: false, error: "No challans found on the request doc. Run save_challans first." };
    }

    const now = new Date();
    const discountMap = new Map<string, AgentDiscount>();
    for (const d of incoming) {
        discountMap.set(d.challanId, d);
    }

    // Update challans array with quotation data
    let totalSettlementAmount = 0;
    let matched = 0;

    const updatedChallans = existingChallans.map((challan: any) => {
        const discount = discountMap.get(challan.id);
        if (discount && discount.discountAmount != null) {
            matched++;
            totalSettlementAmount += discount.discountAmount;
            return {
                ...challan,
                quotation: {
                    amount: discount.discountAmount,
                    at: Timestamp.fromDate(now),
                    settlementAmountAdded: true,
                },
            };
        }
        // Challan without a discount — preserve existing quotation if any,
        // and still count its amount toward total if it had one
        if (challan.quotation?.amount != null) {
            totalSettlementAmount += challan.quotation.amount;
        }
        return challan;
    });

    // Write each challan to subChallans sub-collection (mirrors updateChallanQuotations)
    const subChallansRef = db.collection(`challans/${vehicleNumber}/subChallans`);

    const subDocPromises = updatedChallans.map((challan: any) => {
        const discount = discountMap.get(challan.id);
        const subDoc = {
            challanAmount: challan.challanAmount ?? null,
            challanDate: challan.challanDate ?? null,
            challanNo: challan.challanNo ?? null,
            id: challan.id,
            location: challan.location ?? null,
            offence: challan.offence ?? null,
            paymentDetails: challan.paymentDetails ?? null,
            quotation:
                discount && discount.discountAmount != null
                    ? { amount: discount.discountAmount, at: Timestamp.fromDate(now), settlementAmountAdded: true }
                    : challan.quotation ?? null,
            status: challan.status || "unpaid",
            settlementStatus: challan.status || "unpaid",
            type: challan.type ?? null,
        };
        return subChallansRef.doc(challan.id).set(subDoc, { merge: true });
    });

    await Promise.all(subDocPromises);

    // Update main request doc
    await docRef.update({
        challans: updatedChallans,
        status: "amountAdded",
        totalSettlementAmount,
        updatedAt: FieldValue.serverTimestamp(),
        paymentValidTill: Timestamp.fromDate(
            new Date(now.getTime() + 24 * 60 * 60 * 1000)
        ),
    });

    console.log(
        `[FIRESTORE] save_discounts | job=${body.jobId} vehicle=${vehicleNumber} matched=${matched}/${incoming.length} total=₹${totalSettlementAmount}`
    );

    return {
        ok: true,
        matched,
        total: incoming.length,
        totalSettlementAmount,
        vehicle: vehicleNumber,
        docId: snapshot.docs[0]!.id,
    };
}
