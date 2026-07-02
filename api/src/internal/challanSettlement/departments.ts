import { challanRequestsRef } from "../../firebase";

/**
 * Maps a state code (first 2 letters of a challan id) to its Virtual Courts
 * "Select Department" option text. Shared by:
 *   - the AI challan-settlement prompt (Phase 1.5 department mapping), and
 *   - the scripted challan-settlement runner (which reads departments from
 *     the DB instead of scraping Delhi Traffic Police).
 *
 * KEEP IN SYNC with worker/src/scripted/challan/dispatch.py (_PREFIX_TO_DEPARTMENT).
 */
export const STATE_TO_DEPARTMENT: Record<string, string> = {
    DL: "Delhi(Traffic Department)",
    HR: "Haryana(Traffic Department)",
    UP: "Uttar Pradesh(Traffic Department)",
    CH: "Chandigarh(Traffic Department)",
    RJ: "Rajasthan(Traffic Department)",
    PB: "Punjab(Traffic Department)",
    MP: "Madhya Pradesh(Traffic Department)",
    MH: "Maharashtra(Transport Department)",
    GJ: "Gujarat(Traffic Department)",
    KA: "Karnataka(Traffic Department)",
    HP: "Himachal Pradesh(Traffic Department)",
    UK: "Uttarakhand(Traffic Department)",
    CG: "Chhattisgarh(Traffic Department)",
    JK: "Jammu and Kashmir(Jammu Traffic Department)",
    AS: "Assam(Traffic Department)",
    KL: "Kerala(Police Department)",
    TN: "Tamil Nadu(Traffic Department)",
    AP: "Andhra Pradesh(Traffic Department)",
    TS: "Telangana(Traffic Department)",
    TG: "Telangana(Traffic Department)",
    BR: "Bihar(Traffic Department)",
    JH: "Jharkhand(Traffic Department)",
    OD: "Odisha(Traffic Department)",
    WB: "West Bengal(Traffic Department)",
    GA: "Goa(Traffic Department)",
};

/**
 * Read the challans pre-populated on the request doc (by the app/backend) and
 * map each to its Virtual Courts department name. Returns a UNIQUE, deduplicated
 * list of department names.
 *
 * `challans` is the upstream-populated field (NOT `challansDraft`, which is the
 * automation's own working set). A digit-leading id maps to Delhi(Notice Department).
 */
export const challansFromDB = async (p: Record<string, string>): Promise<string[]> => {
    try {
        const requestId = p.requestId;
        if (!requestId) return [];

        const docSnap = await challanRequestsRef.doc(requestId).get();
        if (!docSnap.exists) return [];

        const docData = docSnap.data()!;
        const existingChallans: any[] = docData.challans || [];

        const depts = new Set<string>();
        for (const c of existingChallans) {
            const id = (c.id || c.challanNo || "").toString();
            if (!id) continue;
            const prefix = id.substring(0, 2).toUpperCase();
            if (/^[A-Z]{2}$/.test(prefix) && STATE_TO_DEPARTMENT[prefix]) {
                depts.add(STATE_TO_DEPARTMENT[prefix]);
            } else if (/^\d/.test(id)) {
                depts.add("Delhi(Notice Department)");
            }
        }
        const allDeps = Array.from(depts);
        console.log("depsFromDB: ", allDeps.length);
        return allDeps;
    } catch (e) {
        console.error("[challansFromDB] error:", e);
        return [];
    }
};
