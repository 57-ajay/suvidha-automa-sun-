import { FieldValue, Timestamp } from "firebase-admin/firestore";
import { getStorage } from "firebase-admin/storage";
import { challanRequestsRef } from "../../firebase";

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

    // The receipt PDF (and its URL) is the deliverable — receiptNumber/amount are
    // best-effort metadata and must NOT block saving the receipt. The challan
    // being paid is identified by challanNo, which we always have from params.
    const challanNo = receiptData.challanNo ?? params?.challanNo ?? null;
    const receiptNumber = receiptData.receiptNumber || null;
    const amount = toNumber(receiptData.amount); // may be null — informational only

    if (!challanNo) {
        console.log(`[save_challan_receipt] FAIL: challanNo missing`);
        return { ok: false, error: "challanNo missing — cannot locate the challan to attach the receipt" };
    }

    // Filename base: receiptNumber if known, else challanNo, else requestId.
    const fileBase = String(receiptNumber || challanNo || requestId).replace(/[^A-Za-z0-9._-]/g, "_");

    // ── Upload PDF to GCS ──────────────────────────────────────────────────
    let pdfUrl: string | null = null;
    let pdfUploadError: string | null = null;

    try {
        const bucket = getStorage().bucket();
        const destination =
            `driverUtilitiesRequests/challanPaymentRequests/` +
            `${requestId}_${resolvedDriverId}/${fileBase}_receipt.pdf`;

        const file = bucket.file(destination);
        await file.save(pdfBuffer, {
            metadata: {
                contentType: "application/pdf",
                metadata: {
                    vehicleNumber,
                    challanNo: String(challanNo),
                    ...(receiptNumber ? { receiptNumber } : {}),
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

    // The whole point is to persist the receipt URL — if the upload failed there
    // is nothing to save, so report failure (the caller marks the job partial).
    if (!pdfUrl) {
        console.log(`[save_challan_receipt] FAIL: PDF upload failed, no URL to attach`);
        return {
            ok: false,
            error: `PDF upload failed: ${pdfUploadError ?? "unknown"}`,
            vehicle: vehicleNumber,
        };
    }

    // ── Attach the receipt to the matching challan ─────────────────────────
    // Challan receipts live PER-CHALLAN as `challans[].receipt = { url, at }`
    // (see Cabswale-Customers data model + driverUtilitiesRequests.js writer),
    // not at the doc level. We only save the receipt URL here — we do NOT mark
    // the challan/request paid; the existing backend trigger handles paid status.
    const docRef = challanRequestsRef.doc(requestId);
    const receiptObj = { url: pdfUrl, at: Timestamp.now() }; // serverTimestamp() is illegal inside array elements
    let attachedTo: "challans" | "challansDraft" | null = null;

    try {
        const snap = await docRef.get();
        if (!snap.exists) {
            return { ok: false, error: `No challanRequest found for requestId ${requestId}`, receiptUrl: pdfUrl };
        }
        const docData = snap.data()!;

        // Match by challanNo or id; prefer the finalized `challans` array, fall
        // back to `challansDraft` if the doc hasn't been finalized yet.
        const matches = (c: any) =>
            String(c?.challanNo) === String(challanNo) || String(c?.id) === String(challanNo);

        const challans: any[] = Array.isArray(docData.challans) ? docData.challans : [];
        const draft: any[] = Array.isArray(docData.challansDraft) ? docData.challansDraft : [];

        const update: Record<string, any> = { updatedAt: FieldValue.serverTimestamp() };

        if (challans.some(matches)) {
            update.challans = challans.map((c) => (matches(c) ? { ...c, receipt: receiptObj } : c));
            attachedTo = "challans";
        } else if (draft.some(matches)) {
            update.challansDraft = draft.map((c) => (matches(c) ? { ...c, receipt: receiptObj } : c));
            attachedTo = "challansDraft";
        } else {
            console.log(
                `[save_challan_receipt] challanNo=${challanNo} not found in challans/challansDraft ` +
                `for requestId=${requestId}`
            );
            return {
                ok: false,
                error: `challanNo ${challanNo} not found in challans/challansDraft for requestId ${requestId}`,
                receiptUrl: pdfUrl,
            };
        }

        await docRef.update(update);
        console.log(
            `[save_challan_receipt] attached receipt.url to ${attachedTo}[challanNo=${challanNo}] ` +
            `in challanRequests/${requestId}`
        );
    } catch (e) {
        console.error(`[save_challan_receipt] failed to attach receipt to challanRequests/${requestId}:`, e);
        return { ok: false, error: `Failed to save receipt: ${(e as Error).message}`, receiptUrl: pdfUrl };
    }

    console.log(
        `[save_challan_receipt] DONE job=${jobId} vehicle=${vehicleNumber} challanNo=${challanNo} ` +
        `receipt=${receiptNumber ?? "n/a"} amount=${amount ?? "n/a"} attachedTo=${attachedTo}`
    );

    return {
        ok: true,
        vehicle: vehicleNumber,
        challanNo,
        receiptNumber,
        amount,
        receiptUrl: pdfUrl,
        attachedTo,
        pdfUploaded: true,
    };
}
