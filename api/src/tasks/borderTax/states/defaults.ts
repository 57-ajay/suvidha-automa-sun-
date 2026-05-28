export type StateDefaults = Record<string, string>;
export const STATE_DEFAULTS: Record<string, StateDefaults> = {
    "UTTAR PRADESH": {
        taxMode: "DAYS",
        entryDistrict: "GHAZIABAD",
        entryCheckpoint: "",
        serviceType: "Air Conditioned Service",
        permitType: "ALL INDIA TOURIST PERMIT",
        permitTypeFallback: "TEMPORARY PERMIT",
        paymentMethod: "net_banking",
        distance: "1000",
    },
    "HARYANA": {
        taxMode: "DAYS",
        entryDistrict: "FARIDABAD",
        entryCheckpoint: "FARIDABAD",
        serviceType: "NOT APPLICABLE",
        permitType: "NOT APPLICABLE",
        permitTypeFallback: "NOT APPLICABLE",
        paymentMethod: "net_banking",
        distance: "1000",
    },
    "RAJASTHAN": {
        taxMode: "DAYS",
        entryDistrict: "CHITTORGARH",
        entryCheckpoint: "",
        serviceType: "NOT APPLICABLE",
        permitType: "TEMPORARY PERMIT",
        permitTypeFallback: "TEMPORARY PERMIT",
        paymentMethod: "net_banking",
        bankName: "State Bank Of India",
    },
    "PUNJAB": {
        taxMode: "DAYS",
        entryDistrict: "MOHALI",
        entryCheckpoint: "",
        serviceType: "NOT APPLICABLE",
        permitType: "NOT APPLICABLE",
        permitTypeFallback: "NOT APPLICABLE",
        paymentMethod: "upi",
    },
    "MADHYA PRADESH": {
        taxMode: "DAYS",
        entryDistrict: "SHEOPUR",
        entryCheckpoint: "",
        serviceType: "Air Conditioned Service",
        permitType: "TEMPORARY PERMIT",
        permitTypeFallback: "TEMPORARY PERMIT",
        paymentMethod: "upi",
    },
    "UTTARAKHAND": {
        taxMode: "DAYS",
        entryDistrict: "DEHRADUN",
        entryCheckpoint: "",
        serviceType: "Air Conditioned Service",
        permitType: "TEMPORARY PERMIT",
        permitTypeFallback: "ALL INDIA TOURIST PERMIT",
        paymentMethod: "net_banking",
    },
};

// ─── Apply defaults ────────────────────────────────────────────────────────

/**
 * Returns a new params object with state-specific defaults filled in for
 * any key that the caller left absent or set to an empty string.
 *
 * Does NOT mutate the original `p`.
 *
 * @param stateKey  Resolved full-name key, e.g. "PUNJAB" (after alias lookup).
 * @param p         Raw params from the API caller.
 */
export function applyStateDefaults(
    stateKey: string,
    p: Record<string, string>,
): Record<string, string> {
    const defaults = STATE_DEFAULTS[stateKey];
    if (!defaults) return { ...p }; // unknown state — pass through unchanged

    const result: Record<string, string> = { ...p };
    for (const [key, value] of Object.entries(defaults)) {
        // Inject only when the caller didn't supply the key (or sent "").
        if (!result[key]) {
            result[key] = value;
        }
    }
    return result;
}
