// api/src/internal/borderTax/vehicleDetails.ts
//
// Resolve the RC record for a vehicle so the worker can fill the parivahan
// form manually when VAHAN's "Get Details" returns no data ("No data found
// for this vehicle number. Please enter the details..").
//
// Resolution order:
//   1. Firestore cache: vehicleDetails/{REGNO}.
//   2. On a miss, TRIGGER a live RC lookup via the invincibleOcean cloud
//      function. That function persists the canonical record to
//      vehicleDetails/{REGNO} server-side and returns a differently-shaped
//      HTTP body. We deliberately IGNORE the body and re-read the persisted
//      doc, so the first request gets byte-for-byte the same record every
//      later cache-hit gets — the shape the worker's manual fill is known to
//      work with. (Using the raw HTTP body directly was the bug: it lacks
//      fields the persisted doc carries, so the first attempt failed while
//      the retry — served from the DB — succeeded.)
//
// The resolved record is attached to the job params as a JSON string
// (`params.vehicleDetails`) in index.ts. Never throws — a miss just means the
// manual-entry fallback has no data and the run fails later with a clear
// abort_reason.

import { db } from "../../firebase";

const RC_API_URL =
    "https://us-central1-bwi-cabswalle.cloudfunctions.net/invincibleOcean-verifyRcDetailsV2";

// The cloud function can hit slow government RC services; give it room.
const RC_API_TIMEOUT_MS = 30_000;

// After the lookup succeeds, the persisted doc is normally readable
// immediately (single-doc gets are strongly consistent), but we poll a few
// times in case the function's write lands a beat after its HTTP response.
const POST_LOOKUP_READ_ATTEMPTS = 6;
const POST_LOOKUP_READ_DELAY_MS = 500;

/** Normalize a registration number the same way the worker does: strip every
 *  non-alphanumeric char and upper-case. "hr38ae7922" / "HR-38-AE-7922" →
 *  "HR38AE7922". */
export function normalizeRegNo(vehicleNumber: string): string {
    return (vehicleNumber || "").replace(/[^A-Za-z0-9]/g, "").toUpperCase();
}

/** Mirror Dart's `DateTime.now().microsecondsSinceEpoch.toString()`. JS has
 *  no microsecond clock, so we scale millis to micros and add a sub-ms random
 *  tail — gives a unique ~16-digit id in the same shape the API expects. */
function makeVerificationId(): string {
    return String(Date.now() * 1000 + Math.floor(Math.random() * 1000));
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Read the cached doc. Returns its data, or null if absent / read fails. */
async function readVehicleDetails(
    regNo: string,
): Promise<Record<string, unknown> | null> {
    try {
        const snap = await db.collection("vehicleDetails").doc(regNo).get();
        return snap.exists ? (snap.data() as Record<string, unknown>) : null;
    } catch (e) {
        console.error(`[vehicleDetails] read failed for ${regNo}:`, e);
        return null;
    }
}

/** Fire the invincibleOcean RC lookup purely to make it persist the record.
 *  Returns true if the lookup succeeded (HTTP ok + success/reg_no in body).
 *  The body itself is intentionally discarded — we read the persisted doc. */
async function triggerRcLookup(regNo: string): Promise<boolean> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), RC_API_TIMEOUT_MS);
    try {
        const res = await fetch(RC_API_URL, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                vehicleNumber: regNo,
                source: "app",
                verificationId: makeVerificationId(),
            }),
            signal: controller.signal,
        });
        if (!res.ok) {
            console.error(`[vehicleDetails] RC API HTTP ${res.status} for ${regNo}`);
            return false;
        }
        const data = (await res.json()) as Record<string, unknown>;
        if (data && (data.success === true || data.reg_no)) {
            console.log(`[vehicleDetails] RC lookup succeeded for ${regNo}`);
            return true;
        }
        console.error(
            `[vehicleDetails] RC API returned no usable data for ${regNo} ` +
            `(success=${data?.success})`,
        );
        return false;
    } catch (e) {
        console.error(`[vehicleDetails] RC API call failed for ${regNo}:`, e);
        return false;
    } finally {
        clearTimeout(timer);
    }
}

/** Resolve the vehicleDetails record: cache first; on a miss, trigger the RC
 *  lookup (which persists the canonical doc) and then read THAT doc back. */
export async function fetchVehicleDetails(
    vehicleNumber: string,
): Promise<Record<string, unknown> | null> {
    const regNo = normalizeRegNo(vehicleNumber);
    if (!regNo) return null;

    // 1. Cache hit — the common path once a vehicle has been looked up once.
    const cached = await readVehicleDetails(regNo);
    if (cached) return cached;

    // 2. Cache miss — trigger the lookup so the function persists the doc.
    const ok = await triggerRcLookup(regNo);
    if (!ok) return null;

    // 3. Re-read the now-persisted doc (the canonical shape). Poll briefly in
    //    case the write lands just after the HTTP response.
    for (let attempt = 1; attempt <= POST_LOOKUP_READ_ATTEMPTS; attempt++) {
        const doc = await readVehicleDetails(regNo);
        if (doc) {
            if (attempt > 1) {
                console.log(
                    `[vehicleDetails] persisted doc for ${regNo} appeared on ` +
                    `read attempt ${attempt}`,
                );
            }
            return doc;
        }
        await sleep(POST_LOOKUP_READ_DELAY_MS);
    }
    console.error(
        `[vehicleDetails] RC lookup for ${regNo} reported success but the ` +
        `persisted doc never appeared after ${POST_LOOKUP_READ_ATTEMPTS} reads ` +
        `(~${(POST_LOOKUP_READ_ATTEMPTS * POST_LOOKUP_READ_DELAY_MS) / 1000}s). ` +
        `The cloud function may persist asynchronously — widen the read window.`,
    );
    return null;
}
