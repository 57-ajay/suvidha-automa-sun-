// api/src/internal/borderTax/driverContact.ts
//
// Resolve a driver's phone number from drivers/{driverId} so it can be used
// as the form's mobile number when the job didn't carry one. `mobileNumber`
// is an optional border-tax param; when the app omits it, index.ts calls this
// to fill it from the driver doc (we always have driverId — it's required).
//
// Field name: the driver doc's phone field isn't referenced anywhere else in
// this codebase, and the only hint we have is the `driverDetails.phoneNo`
// shape on vehicleDetails records, so we try the common variants and take the
// first that looks like a phone (>= 10 digits). If yours is named something
// else, add it to PHONE_FIELDS.

import { db } from "../../firebase";

const PHONE_FIELDS = [
    "phoneNo",
    "phoneNumber",
    "phone",
    "mobileNumber",
    "mobileNo",
    "mobile",
    "contactNumber",
];

/** Return the driver's phone (raw, e.g. "+919064983473") or null. The worker
 *  strips it to the last 10 digits, so country code / spacing don't matter.
 *  Never throws. */
export async function resolveDriverMobile(
    driverId: string,
): Promise<string | null> {
    const id = (driverId || "").trim();
    if (!id) return null;
    try {
        const snap = await db.collection("drivers").doc(id).get();
        if (!snap.exists) {
            console.error(`[driverContact] driver ${id} not found`);
            return null;
        }
        const data = snap.data() || {};
        for (const field of PHONE_FIELDS) {
            const v = data[field];
            if (typeof v === "string" && v.replace(/\D/g, "").length >= 10) {
                console.log(`[driverContact] driver ${id} phone via field '${field}'`);
                return v;
            }
            if (typeof v === "number" && String(v).length >= 10) {
                return String(v);
            }
        }
        console.error(
            `[driverContact] driver ${id} has no recognizable phone field ` +
            `(tried ${PHONE_FIELDS.join(", ")})`,
        );
        return null;
    } catch (e) {
        console.error(`[driverContact] read failed for driver ${id}:`, e);
        return null;
    }
}
