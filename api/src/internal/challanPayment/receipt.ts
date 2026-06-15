import { FieldValue } from "firebase-admin/firestore";
import { getStorage } from "firebase-admin/storage";
import { db, challanRequestsRef } from "../../firebase";

interface ReceiptData {
    vehicleNumber: string;
    receiptNumber: string;
    amount: number;
    paymentDate: string;
    challanNo?: string;
    department?: string;
}

export interface SaveChallanReceiptInput {
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

export async function handleSaveChallanReceipt(input: SaveChallanReceiptInput) {
    const { jobId, params, data, pdfBuffer } = input;

    const driverId = params?.driverId;
    if (!driverId) {
        console.warn(
            `[save_challan_receipt] WARN: driverId missing from params job=${jobId}. ` +
            `Usage tracking will be impaired. Params: ${JSON.stringify(params)}`
        );
    }
    const resolvedDriverId = driverId ?? "unknown";

    console.log(`[save_challan_receipt] START job=${jobId} pdf=${pdfBuffer?.length ?? 0} bytes`);
    console.log(`[save_challan_receipt] params=${JSON.stringify(params)}`);
    console.log(`[save_challan_receipt] data=${JSON.stringify(data)}`);

    const vehicleNumber = params?.vehicleNumber;
    if (!vehicleNumber) {
        console.log(`[save_challan_receipt] FAIL: vehicleNumber missing`);
        return { ok: false, error: "vehicleNumber missing from job params" };
    }

    const requestId = params?.requestId;
    if (!requestId) {
        console.log(`[save_challan_receipt] FAIL: requestId missing`);
        return { ok: false, error: "requestId missing from job params" };
    }

    if (!pdfBuffer || pdfBuffer.length === 0) {
        console.log(`[save_challan_receipt] FAIL: empty PDF buffer`);
        return { ok: false, error: "PDF buffer is empty" };
    }

    // Parse receipt data — accept object, array of one, or JSON string
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

    if (!receiptData?.receiptNumber) {
        return { ok: false, error: "receiptNumber is required" };
    }

    const amount = toNumber(receiptData.amount);
    if (amount === null) {
        return { ok: false, error: `Invalid amount: ${receiptData.amount}` };
    }

    // ── Upload PDF to GCS ──────────────────────────────────────────────────
    let pdfUrl: string | null = null;
    let pdfUploadError: string | null = null;

    try {
        const bucket = getStorage().bucket();
        const destination =
            `driverUtilitiesRequests/challanPaymentRequests/` +
            `${requestId}_${resolvedDriverId}/${receiptData.receiptNumber}_receipt.pdf`;

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
            `[save_challan_receipt] PDF uploaded: ${destination} (${pdfBuffer.length} bytes)`
        );
    } catch (e) {
        pdfUploadError = (e as Error).message;
        console.error(`[save_challan_receipt] PDF upload FAILED:`, e);
        // Continue — still persist metadata even if PDF upload fails
    }

    // ── Save a record in challanPayments collection ────────────────────────
    const challanPaymentsRef = db.collection("challanPayments");
    const docData = {
        driverId: resolvedDriverId,
        vehicleNumber,
        requestId,
        jobId,
        challanNo: receiptData.challanNo ?? params?.challanNo ?? null,
        department: receiptData.department ?? params?.department ?? null,
        receiptNumber: receiptData.receiptNumber,
        amount,
        paymentDate: receiptData.paymentDate || new Date().toISOString().split("T")[0],
        ...(pdfUrl ? { pdfUrl } : {}),
        ...(pdfUploadError ? { pdfUploadError } : {}),
        status: "paid",
        createdAt: FieldValue.serverTimestamp(),
    };

    // ── Update the challanRequests doc ─────────────────────────────────────
    try {
        await challanRequestsRef.doc(requestId).update({
            status: "completed",
            challanUpdatedBy: "agent",
            receiptUpdatedAt: FieldValue.serverTimestamp(),
            paymentDate: FieldValue.serverTimestamp(),
            ...(pdfUrl ? { receiptDocumentUrl: pdfUrl } : {}),
        });
        console.log(
            `[save_challan_receipt] marked challanRequests/${requestId} status=completed ` +
            `(receiptDocumentUrl=${pdfUrl ? "set" : "skipped — pdf upload failed"})`
        );
    } catch (e) {
        console.error(
            `[save_challan_receipt] failed to mark challanRequests/${requestId}:`, e
        );
        // Non-fatal — still save the payment record
    }

    const paymentDoc = await challanPaymentsRef.add(docData);

    console.log(
        `[save_challan_receipt] DONE job=${jobId} vehicle=${vehicleNumber} ` +
        `receipt=${receiptData.receiptNumber} amount=₹${amount} ` +
        `doc=${paymentDoc.id} pdfUploaded=${!!pdfUrl}`
    );

    return {
        ok: true,
        vehicle: vehicleNumber,
        receiptNumber: receiptData.receiptNumber,
        amount,
        docId: paymentDoc.id,
        pdfUploaded: !!pdfUrl,
        ...(pdfUploadError ? { pdfUploadError } : {}),
    };
}
