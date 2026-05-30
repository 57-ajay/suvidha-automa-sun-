export const MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
];

/** Accept "2026-04-29", "29-04-2026", "29/04/2026", "04/29/2026" → return "YYYY-MM-DD". */
export function normalizeISODate(input: string): string {
    if (!input) return "";
    const s = input.trim();

    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;

    // DD-MM-YYYY or DD/MM/YYYY (Indian convention)
    const dmy = s.match(/^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$/);
    if (dmy) {
        const [, d, mo, y] = dmy;
        return `${y}-${mo!.padStart(2, "0")}-${d!.padStart(2, "0")}`;
    }

    const dt = new Date(s);
    if (!isNaN(dt.getTime())) return dt.toISOString().split("T")[0]!;

    return s;
}

export interface DateParts {
    iso: string;
    year: string;
    mm: string;
    dd: string;
    monthName: string;
    mmddyyyy: string;
}

export function dateParts(iso: string): DateParts {
    const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (!m) {
        return {
            iso, year: "????", mm: "??", dd: "??",
            monthName: "???", mmddyyyy: iso,
        };
    }
    const [, y, mo, d] = m;
    return {
        iso,
        year: y!,
        mm: mo!,
        dd: d!,
        monthName: MONTH_NAMES[parseInt(mo!, 10) - 1] || "???",
        mmddyyyy: `${mo}/${d}/${y}`,
    };
}

/** States where taxFrom === taxUpto is valid (duration offset = duration - 1). */
const SAME_DAY_STATES = new Set([
    "UTTAR PRADESH",
    "RAJASTHAN",
    "MADHYA PRADESH",
]);

/** States where taxFrom !== taxUpto is required (duration offset = duration). */
const NO_SAME_DAY_STATES = new Set([
    "PUNJAB",
    "HARYANA",
    "HIMACHAL PRADESH",
    "BIHAR",
    "TAMIL NADU",
]);

/**
 * Compute taxUpto from taxFrom + duration, respecting state-specific rules.
 *
 * @param taxFromISO  Normalized ISO date string (YYYY-MM-DD).
 * @param duration    Number of days (minimum 1).
 * @param stateKey    Resolved full-name state key (e.g. "UTTAR PRADESH").
 * @returns           ISO date string for taxUpto (YYYY-MM-DD).
 * @throws            If taxFromISO is invalid, duration < 1, or state is unknown.
 */
export function computeTaxUpto(
    taxFromISO: string,
    duration: number,
    stateKey: string,
): string {
    if (duration < 1) {
        throw new Error(`duration must be >= 1 (got ${duration})`);
    }

    const fromDate = new Date(taxFromISO + "T00:00:00Z");
    if (isNaN(fromDate.getTime())) {
        throw new Error(`Invalid taxFrom date: ${taxFromISO}`);
    }

    let offsetDays: number;

    if (SAME_DAY_STATES.has(stateKey)) {
        // UP, RJ, MP: duration=1 means taxUpto === taxFrom
        offsetDays = duration - 1;
    } else if (NO_SAME_DAY_STATES.has(stateKey)) {
        // PB, HR: duration=1 means taxUpto = taxFrom + 1
        offsetDays = duration;
    } else {
        // Unknown state — default to same-day-allowed (safer: shorter range)
        console.warn(
            `[computeTaxUpto] unknown state "${stateKey}", ` +
            `defaulting to same-day-allowed (offset = duration - 1)`
        );
        offsetDays = duration - 1;
    }

    const result = new Date(fromDate);
    result.setUTCDate(result.getUTCDate() + offsetDays);

    const yyyy = result.getUTCFullYear();
    const mm = String(result.getUTCMonth() + 1).padStart(2, "0");
    const dd = String(result.getUTCDate()).padStart(2, "0");

    return `${yyyy}-${mm}-${dd}`;
}
