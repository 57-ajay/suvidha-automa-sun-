import { FieldValue } from "firebase-admin/firestore";
import { getStorage } from "firebase-admin/storage";
import { borderTaxRequestsRef } from "../../firebase";

export interface SaveQRInput {
    jobId: string;
    params: Record<string, string>;
    imageBuffer: Buffer;
}

export async function handleSaveQR(input: SaveQRInput) {
    const { jobId, params, imageBuffer } = input;
    const requestId = params?.requestId;
    const vehicleNumber = params?.vehicleNumber;
    const driverId = params?.driverId ?? "unknown";

    console.log(`[save_qr] START job=${jobId} image=${imageBuffer?.length ?? 0} bytes`);
    console.log(`[save_qr] params=${JSON.stringify(params)}`);

    if (!requestId) {
        console.log(`[save_qr] FAIL: requestId missing`);
        return { ok: false, error: "requestId missing from job params" };
    }

    if (!vehicleNumber) {
        console.log(`[save_qr] FAIL: vehicleNumber missing`);
        return { ok: false, error: "vehicleNumber missing from job params" };
    }

    if (!imageBuffer || imageBuffer.length === 0) {
        console.log(`[save_qr] FAIL: empty image buffer`);
        return { ok: false, error: "image buffer is empty" };
    }

    // ── Upload PNG to GCS ──
    let qrUrl: string | null = null;

    try {
        const bucket = getStorage().bucket();
        // Store alongside the receipt so all job artefacts are co-located
        const destination = `driverUtilitiesRequests/borderTaxRequests/${requestId}_${driverId}/qr_code.png`;

        const file = bucket.file(destination);
        await file.save(imageBuffer, {
            metadata: {
                contentType: "image/png",
                metadata: {
                    vehicleNumber,
                    jobId,
                },
            },
        });

        // QR codes expire in ~3 minutes on SBIePay, so a 3-minute signed URL
        // is enough for the client to display it; no longer needed after payment.
        const [url] = await file.getSignedUrl({
            action: "read",
            expires: Date.now() + 3 * 60 * 1000, // 3 minutes
        });

        qrUrl = url;
        console.log(
            `[save_qr] PNG uploaded: ${destination} (${imageBuffer.length} bytes)`
        );
    } catch (e) {
        const msg = (e as Error).message;
        console.error(`[save_qr] PNG upload FAILED:`, e);
        return { ok: false, error: `GCS upload failed: ${msg}` };
    }

    // ── Write qrCodeUrl to borderTaxRequests/{requestId} ──
    try {
        await borderTaxRequestsRef.doc(requestId).update({
            qrCodeUrl: qrUrl,
            qrUploadedAt: FieldValue.serverTimestamp(),
            aiAgentWorkStatus: "qrUrlAdded",
        });
        console.log(
            `[save_qr] borderTaxRequests/${requestId} updated with qrCodeUrl`
        );
    } catch (e) {
        const msg = (e as Error).message;
        console.error(`[save_qr] Firestore update FAILED:`, e);
        // GCS upload succeeded — don't lose the URL; still fail so worker knows
        return { ok: false, error: `Firestore update failed: ${msg}` };
    }

    console.log(`[save_qr] DONE job=${jobId} vehicle=${vehicleNumber} requestId=${requestId}`);

    return { ok: true, qrUploaded: true, qrUrl };
}
