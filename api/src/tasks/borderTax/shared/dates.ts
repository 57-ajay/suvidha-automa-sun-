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
