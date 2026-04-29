import { FieldValue } from "firebase-admin/firestore";
import { getStorage } from "firebase-admin/storage";
import { db, borderTaxRequestsRef } from "../../firebase";

interface ReceiptData {
    vehicleNumber: string;
    receiptNumber: string;
    amount: number;
    paymentDate: string;
}

export interface SaveReceiptInput {
    jobId: string;
    params: Record<string, string>;
    data: unknown;
    pdfBuffer: Buffer;
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

export async function handleSaveReceipt(input: SaveReceiptInput) {
    const { jobId, params, data, pdfBuffer } = input;
    const driverId = params?.driverId ?? "driverId";

    console.log(`[save_receipt] START job=${jobId} pdf=${pdfBuffer?.length ?? 0} bytes`);
    console.log(`[save_receipt] params=${JSON.stringify(params)}`);
    console.log(`[save_receipt] data=${JSON.stringify(data)}`);

    const vehicleNumber = params?.vehicleNumber;
    if (!vehicleNumber) {
        console.log(`[save_receipt] FAIL: vehicleNumber missing`);
        return { ok: false, error: "vehicleNumber missing from job params" };
    }

    const requestId = params?.requestId;
    if (!requestId) {
        console.log(`[save_receipt] FAIL: requestId missing`);
        return { ok: false, error: "requestId missing from job params" };
    }

    if (!pdfBuffer || pdfBuffer.length === 0) {
        console.log(`[save_receipt] FAIL: empty PDF buffer`);
        return { ok: false, error: "PDF buffer is empty" };
    }

    // Parse receipt data — defensive: accept object, array of one, or JSON string
    let receiptData: ReceiptData;

    if (typeof data === "string") {
        try {
            const parsed = JSON.parse(data);
            receiptData = Array.isArray(parsed) ? parsed[0] : parsed;
        } catch (e) {
            return { ok: false, error: `data is not valid JSON: ${(e as Error).message}` };
        }
    } else if (Array.isArray(data)) {
        receiptData = data[0] as ReceiptData;
    } else if (typeof data === "object" && data !== null) {
        receiptData = data as ReceiptData;
    } else {
        return { ok: false, error: `Invalid data type: ${typeof data}` };
    }

    if (!receiptData || !receiptData.receiptNumber) {
        return { ok: false, error: "receiptNumber is required" };
    }

    const amount = toNumber(receiptData.amount);
    if (amount === null) {
        return { ok: false, error: `Invalid amount: ${receiptData.amount}` };
    }

    // ── Upload PDF buffer directly to GCS (no disk involvement) ──
    let pdfUrl: string | null = null;
    let pdfUploadError: string | null = null;

    try {
        const bucket = getStorage().bucket();
        const destination = `driverUtilities/stateTaxRequests/${requestId}_${driverId}/${receiptData.receiptNumber}_receipt.pdf`;

        const file = bucket.file(destination);
        await file.save(pdfBuffer, {
            metadata: {
                contentType: "application/pdf",
                metadata: {
                    vehicleNumber,
                    receiptNumber: receiptData.receiptNumber,
                    jobId,
                },
            },
        });

        const [url] = await file.getSignedUrl({
            action: "read",
            expires: Date.now() + 365 * 24 * 60 * 60 * 1000, // 1 year
        });

        pdfUrl = url;
        console.log(
            `[save_receipt] PDF uploaded: ${destination} (${pdfBuffer.length} bytes)`
        );
    } catch (e) {
        pdfUploadError = (e as Error).message;
        console.error(`[save_receipt] PDF upload FAILED:`, e);
        // Continue — we still want to save the receipt metadata even if PDF upload fails
    }

    // ── Save receipt metadata to Firestore ──
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
        ...(pdfUploadError ? { pdfUploadError } : {}),
        status: "paid",
        createdAt: FieldValue.serverTimestamp(),
    };

    // Mark request as agent-updated (best-effort)
    try {
        await borderTaxRequestsRef.doc(requestId).update({
            borderTaxUpdatedBy: "agent",
        });
        console.log(`[save_receipt] marked borderTaxRequests/${requestId} as agent-updated`);
    } catch (e) {
        console.error(`[save_receipt] failed to mark borderTaxRequests/${requestId}:`, e);
    }

    const docRef = await borderTaxRef.add(docData);

    console.log(
        `[save_receipt] DONE job=${jobId} vehicle=${vehicleNumber} ` +
        `receipt=${receiptData.receiptNumber} amount=₹${amount} ` +
        `doc=${docRef.id} pdfUploaded=${!!pdfUrl}`
    );

    return {
        ok: true,
        vehicle: vehicleNumber,
        receiptNumber: receiptData.receiptNumber,
        amount,
        docId: docRef.id,
        pdfUploaded: !!pdfUrl,
        ...(pdfUploadError ? { pdfUploadError } : {}),
    };
}
