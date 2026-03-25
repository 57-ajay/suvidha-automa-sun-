import { Timestamp, FieldValue } from "firebase-admin/firestore";
import { challanRequestsRef } from "../firebase";

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

function parseDate(dateStr: string): Timestamp {
    const parsed = new Date(dateStr);
    if (isNaN(parsed.getTime())) {
        // Fallback: if agent sends something unparseable, use now
        return Timestamp.now();
    }
    return Timestamp.fromDate(parsed);
}

export async function handleSaveChallans(body: InternalRequest) {
    const vehicleNumber = body.params?.vehicleNumber;
    if (!vehicleNumber) {
        return { ok: false, error: "vehicleNumber missing from job params" };
    }

    const incoming = body.data as AgentChallan[];
    if (!Array.isArray(incoming) || incoming.length === 0) {
        return { ok: false, error: "data must be a non-empty array of challans" };
    }

    // Validate each challan before touching Firestore
    for (const c of incoming) {
        if (!c.challanId || typeof c.challanId !== "string") {
            return { ok: false, error: `Invalid challanId: ${JSON.stringify(c)}` };
        }
        if (typeof c.amount !== "number" || c.amount <= 0) {
            return { ok: false, error: `Invalid amount for challan ${c.challanId}: ${c.amount}` };
        }
    }

    // Find the challanRequest doc for this vehicle
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
        return {
            challanAmount: c.amount,
            challanDate: parseDate(c.date),
            challanNo: c.challanId,
            id: c.challanId,
            isSelected: true,
            offence: c.offence || null,
            // Preserve quotation if it already existed
            ...(existing?.quotation ? { quotation: existing.quotation } : {}),
        };
    });

    await docRef.update({
        challans: mergedChallans,
        updatedAt: FieldValue.serverTimestamp(),
    });

    console.log(
        `[FIRESTORE] save_challans | job=${body.jobId} vehicle=${vehicleNumber} saved=${mergedChallans.length}`
    );

    return {
        ok: true,
        saved: mergedChallans.length,
        vehicle: vehicleNumber,
        docId: snapshot.docs[0]!.id,
    };
}
