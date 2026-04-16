import { FieldValue } from "firebase-admin/firestore";
import { getStorage } from "firebase-admin/storage";
import { db } from "../../firebase";
import * as fs from "fs";

interface ReceiptData {
    vehicleNumber: string;
    receiptNumber: string;
    amount: number;
    paymentDate: string;
}

interface InternalRequest {
    jobId: string;
    params: Record<string, string>;
    data: unknown;
}

/** Coerce a value to a number */
function toNumber(val: unknown): number | null {
    if (typeof val === "number") return val;
    if (typeof val === "string") {
        const n = Number(val);
        return isNaN(n) ? null : n;
    }
    return null;
}

export async function handleSaveReceipt(body: InternalRequest) {
    const jobId = body.jobId ?? "unknown";
    const driverId = body.params?.driverId ?? "driverId";

    console.log(`[save_receipt] START job=${jobId}`);
    console.log(`[save_receipt] params=${JSON.stringify(body.params)}`);
    console.log(`[save_receipt] data=${JSON.stringify(body.data)}`);

    const vehicleNumber = body.params?.vehicleNumber;
    if (!vehicleNumber) {
        console.log(`[save_receipt] FAIL: vehicleNumber missing`);
        return { ok: false, error: "vehicleNumber missing from job params" };
    }

    const requestId = body.params?.requestId;
    if (!requestId) {
        console.log(`[save_receipt] FAIL: requestId missing`);
        return { ok: false, error: "requestId missing from job params" };
    }

    // Parse receipt data
    let receiptData: ReceiptData;

    if (typeof body.data === "string") {
        try {
            const parsed = JSON.parse(body.data);
            receiptData = Array.isArray(parsed) ? parsed[0] : parsed;
        } catch (e) {
            return { ok: false, error: `data is not valid JSON: ${(e as Error).message}` };
        }
    } else if (Array.isArray(body.data)) {
        receiptData = body.data[0] as ReceiptData;
    } else if (typeof body.data === "object" && body.data !== null) {
        receiptData = body.data as ReceiptData;
    } else {
        return { ok: false, error: `Invalid data type: ${typeof body.data}` };
    }

    if (!receiptData || !receiptData.receiptNumber) {
        return { ok: false, error: "receiptNumber is required" };
    }

    const amount = toNumber(receiptData.amount);
    if (amount === null) {
        return { ok: false, error: `Invalid amount: ${receiptData.amount}` };
    }

    // Try to upload PDF if it exists on disk
    let pdfUrl: string | null = null;
    const pdfPath = `/app/receipts/${vehicleNumber}.pdf`;

    try {
        if (fs.existsSync(pdfPath)) {
            const bucket = getStorage().bucket();
            const destination = `driverUtilities/stateTaxRequests/${requestId}_${driverId}/${receiptData.receiptNumber}_receipt.pdf`;

            await bucket.upload(pdfPath, {
                destination,
                metadata: {
                    contentType: "application/pdf",
                    metadata: {
                        vehicleNumber,
                        receiptNumber: receiptData.receiptNumber,
                        jobId,
                    },
                },
            });

            const file = bucket.file(destination);
            const [url] = await file.getSignedUrl({
                action: "read",
                expires: Date.now() + 365 * 24 * 60 * 60 * 1000, // 1 year
            });

            pdfUrl = url;
            console.log(`[save_receipt] PDF uploaded: ${destination}`);
        } else {
            console.log(`[save_receipt] No PDF found at ${pdfPath}, saving metadata only`);
        }
    } catch (e) {
        console.error(`[save_receipt] PDF upload failed:`, e);
        // Continue — save metadata even if PDF upload fails
    }

    // Save to Firestore
    const borderTaxRef = db.collection("borderTaxPayments");
    const docData = {
        vehicleNumber,
        requestId,
        jobId,
        receiptNumber: receiptData.receiptNumber,
        amount,
        paymentDate: receiptData.paymentDate || new Date().toISOString().split("T")[0],
        state: "UTTAR PRADESH",
        ...(pdfUrl ? { pdfUrl } : {}),
        status: "paid",
        createdAt: FieldValue.serverTimestamp(),
    };

    const docRef = await borderTaxRef.add(docData);

    console.log(
        `[save_receipt] SUCCESS job=${jobId} vehicle=${vehicleNumber} receipt=${receiptData.receiptNumber} amount=₹${amount} doc=${docRef.id}`
    );

    return {
        ok: true,
        vehicle: vehicleNumber,
        receiptNumber: receiptData.receiptNumber,
        amount,
        docId: docRef.id,
        pdfUploaded: !!pdfUrl,
    };
}
