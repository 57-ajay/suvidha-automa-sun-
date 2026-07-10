import { FieldValue, Timestamp } from "firebase-admin/firestore";
import { getStorage } from "firebase-admin/storage";
import { challanRequestsRef, subChallanRequestsRef, db } from "../../firebase";

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

    // ── Upload PDF to Firebase Storage ─────────────────────────────────────
    // Match the EXISTING challan-receipt convention exactly (verified against
    // real completed challanRequests docs):
    //   path: driverUtilitiesRequests/challan/{requestId}/{challanNo}_{ms}_receipt.pdf
    //   url:  permanent Firebase Storage download URL (?alt=media&token=<uuid>)
    // — NOT a getSignedUrl() signed URL (different domain + it expires).
    let pdfUrl: string | null = null;
    let pdfUploadError: string | null = null;

    try {
        const bucket = getStorage().bucket();
        const destination =
            `driverUtilitiesRequests/challan/${requestId}/` +
            `${fileBase}_${Date.now()}_receipt.pdf`;
        const downloadToken = crypto.randomUUID();

        const file = bucket.file(destination);
        await file.save(pdfBuffer, {
            metadata: {
                contentType: "application/pdf",
                metadata: {
                    firebaseStorageDownloadTokens: downloadToken,
                    vehicleNumber,
                    challanNo: String(challanNo),
                    ...(receiptNumber ? { receiptNumber } : {}),
                    jobId,
                },
            },
        });

        pdfUrl =
            `https://firebasestorage.googleapis.com/v0/b/${bucket.name}/o/` +
            `${encodeURIComponent(destination)}?alt=media&token=${downloadToken}`;
        console.log(
            `[save_challan_receipt] PDF uploaded: ${destination} (${pdfBuffer.length} bytes)`
        );
    } catch (e) {
        pdfUploadError = (e as Error).message;
        console.error(`[save_challan_receipt] PDF upload FAILED:`, e);
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
    // v1 (challanRequests): receipts live PER-CHALLAN as `challans[].receipt =
    // { url, at }` (see Cabswale-Customers data model + driverUtilitiesRequests.js
    // writer). New flow (subChallanRequests): one challan per doc, so receipt is
    // written at the doc top level (handled just below). Either way we only save
    // the receipt URL — we do NOT mark it paid; the existing backend trigger does.
    const receiptObj = { url: pdfUrl, at: Timestamp.now() }; // serverTimestamp() is illegal inside array elements
    let attachedTo: "challans" | "challansDraft" | "subChallanRequests" | null = null;

    // New challan flow: subChallanRequests is one-challan-per-doc, keyed by doc id
    // (== requestId). Write receipt at the doc top level; challanNo is NOT unique
    // in that collection so it can't be used to locate the doc.
    const isNewChallanFlow = (params as Record<string, unknown>)?.isNewChallanFlow === true;
    if (isNewChallanFlow) {
        try {
            await subChallanRequestsRef.doc(requestId).update({
                receipt: receiptObj,
                updatedAt: FieldValue.serverTimestamp(),
            });
            attachedTo = "subChallanRequests";
            console.log(
                `[save_challan_receipt] (new-flow) attached receipt.url to subChallanRequests/${requestId}`
            );
        } catch (e) {
            console.error(
                `[save_challan_receipt] (new-flow) failed to attach receipt to subChallanRequests/${requestId}:`, e
            );
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

    const docRef = challanRequestsRef.doc(requestId);
    // Match by challanNo or id; prefer the finalized `challans` array, fall
    // back to `challansDraft` if the doc hasn't been finalized yet.
    const matches = (c: any) =>
        String(c?.challanNo) === String(challanNo) || String(c?.id) === String(challanNo);

    try {
        // Transaction: jobId == challanNo lets multiple challans in the SAME doc
        // run as concurrent jobs, so two whole-array writes would clobber each
        // other's entries. Read-modify-write the challans[] array atomically.
        const outcome = await db.runTransaction(async (tx) => {
            const snap = await tx.get(docRef);
            if (!snap.exists) return { kind: "no_doc" as const };
            const docData = snap.data()!;

            const challans: any[] = Array.isArray(docData.challans) ? docData.challans : [];
            const draft: any[] = Array.isArray(docData.challansDraft) ? docData.challansDraft : [];

            if (challans.some(matches)) {
                tx.update(docRef, {
                    challans: challans.map((c) => (matches(c) ? { ...c, receipt: receiptObj } : c)),
                    updatedAt: FieldValue.serverTimestamp(),
                });
                return { kind: "ok" as const, attachedTo: "challans" as const };
            }
            if (draft.some(matches)) {
                tx.update(docRef, {
                    challansDraft: draft.map((c) => (matches(c) ? { ...c, receipt: receiptObj } : c)),
                    updatedAt: FieldValue.serverTimestamp(),
                });
                return { kind: "ok" as const, attachedTo: "challansDraft" as const };
            }
            return { kind: "not_in_array" as const };
        });

        if (outcome.kind === "no_doc") {
            return { ok: false, error: `No challanRequest found for requestId ${requestId}`, receiptUrl: pdfUrl };
        }
        if (outcome.kind === "not_in_array") {
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

        attachedTo = outcome.attachedTo;
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
