// api/src/internal/borderTax/driverUsage.ts
//
// Single source of truth for all border-tax eligibility, block tracking,
// and usage-counter logic.
//
// Firestore shape
// ───────────────
//   driverUtilitiesRequests/borderTaxSummary/{driverId}/borderTaxUsage
//   {
//     dailyCount:        number,          // resets each IST calendar day
//     monthlyCount:      number,          // resets each IST calendar month
//     lastDayKey:        string,          // "YYYY-MM-DD" IST — rollover sentinel
//     lastMonthKey:      string,          // "YYYY-MM"    IST — rollover sentinel
//     totalAllTimePaid:  number,          // lifetime paid count (for non-members)
//
//     blockType:         "qr_generated" | "process_failed" | "paid_today" | null,
//     blockExpiresAt:    Timestamp | null,
//     blockReason:       string   | null,
//     blockedAt:         Timestamp | null,
//     lastRequestId:     string   | null,  // requestId that caused the last block
//
//     updatedAt:         Timestamp,
//   }
//
// Block semantics
// ───────────────
//   process_failed  — agent ran, NO QR generated (bad docs / gov site down)
//                     → driver blocked for 24 hours
//   qr_generated    — agent ran, QR WAS generated but payment was not made
//                     → driver blocked for 45 minutes
//   paid_today      — driver paid successfully today
//                     → blocked until next IST midnight (can't pay twice in one day)
//
// Counter semantics
// ─────────────────
//   Eligibility API → READ ONLY, never writes counters.
//   job-completed callback → increments counters ONLY on status=done.
//   This prevents phantom increments if the eligibility check fires but
//   the job never actually runs.

import { FieldValue, Timestamp } from "firebase-admin/firestore";
import { db, borderTaxRequestsRef, driverBorderTaxUsageRef } from "../../firebase";

// ─── Constants ────────────────────────────────────────────────────────────────

const IST_OFFSET_MS = 5.5 * 60 * 60 * 1000; // UTC+5:30

const DAILY_LIMIT_MEMBERS = 1;   // max 1 border tax per day (members)
const MONTHLY_LIMIT_MEMBERS = 30;  // max 30 border taxes per month (members)
const FREE_LIFETIME_LIMIT = 1;   // max 1 border tax ever (non-members)

const BLOCK_PROCESS_FAILED_MS = 24 * 60 * 60 * 1000; // 24 hours
const BLOCK_QR_GENERATED_MS = 45 * 60 * 1000;       // 45 minutes

// ─── Types ────────────────────────────────────────────────────────────────────

export type BlockType = "qr_generated" | "process_failed" | "paid_today" | null;

export interface DriverUsageSummary {
    dailyCount: number;
    monthlyCount: number;
    lastDayKey: string;
    lastMonthKey: string;
    totalAllTimePaid: number;
    blockType: BlockType;
    blockExpiresAt: Timestamp | null;
    blockReason: string | null;
    blockedAt: Timestamp | null;
    lastRequestId: string | null;
    updatedAt: Timestamp | null;
}

export interface EligibilityResult {
    eligible: boolean;
    reason?: string;       // human-readable denial reason
    blockType?: BlockType;
    unblockAt?: Date;         // when the block lifts
    membershipActive?: boolean;
}

// ─── IST helpers ──────────────────────────────────────────────────────────────

function nowIST(): Date {
    return new Date(Date.now() + IST_OFFSET_MS);
}

function istDayKey(date: Date = nowIST()): string {
    const y = date.getUTCFullYear();
    const m = String(date.getUTCMonth() + 1).padStart(2, "0");
    const d = String(date.getUTCDate()).padStart(2, "0");
    return `${y}-${m}-${d}`;
}

function istMonthKey(date: Date = nowIST()): string {
    const y = date.getUTCFullYear();
    const m = String(date.getUTCMonth() + 1).padStart(2, "0");
    return `${y}-${m}`;
}

/** Next IST midnight expressed as a UTC Date */
function nextISTMidnight(): Date {
    const ist = nowIST();
    // Build "tomorrow 00:00:00 IST" in UTC space
    const tomorrowIST = new Date(Date.UTC(
        ist.getUTCFullYear(),
        ist.getUTCMonth(),
        ist.getUTCDate() + 1,
        0, 0, 0, 0,
    ));
    // Shift back to UTC: midnight IST = (midnight IST - 5h30m) UTC
    return new Date(tomorrowIST.getTime() - IST_OFFSET_MS);
}

// ─── Membership check ─────────────────────────────────────────────────────────

async function getMembershipActive(driverId: string): Promise<boolean | null> {
    const snap = await db.collection("drivers").doc(driverId).get();
    if (!snap.exists) return null; // driver not found
    return snap.data()?.membership?.active === true;
}

// ─── Read + normalize usage doc ───────────────────────────────────────────────

async function readSummary(driverId: string): Promise<DriverUsageSummary> {
    const snap = await driverBorderTaxUsageRef(driverId).get();
    if (!snap.exists) {
        return {
            dailyCount: 0,
            monthlyCount: 0,
            lastDayKey: "",
            lastMonthKey: "",
            totalAllTimePaid: 0,
            blockType: null,
            blockExpiresAt: null,
            blockReason: null,
            blockedAt: null,
            lastRequestId: null,
            updatedAt: null,
        };
    }
    const d = snap.data()!;
    return {
        dailyCount: d.dailyCount ?? 0,
        monthlyCount: d.monthlyCount ?? 0,
        lastDayKey: d.lastDayKey ?? "",
        lastMonthKey: d.lastMonthKey ?? "",
        totalAllTimePaid: d.totalAllTimePaid ?? 0,
        blockType: d.blockType ?? null,
        blockExpiresAt: d.blockExpiresAt ?? null,
        blockReason: d.blockReason ?? null,
        blockedAt: d.blockedAt ?? null,
        lastRequestId: d.lastRequestId ?? null,
        updatedAt: d.updatedAt ?? null,
    };
}

// ─── Roll-over (pure, no Firestore write) ────────────────────────────────────

function applyRollover(summary: DriverUsageSummary): DriverUsageSummary {
    const dayKey = istDayKey();
    const monthKey = istMonthKey();
    let { dailyCount, monthlyCount, lastDayKey, lastMonthKey } = summary;

    if (lastMonthKey !== monthKey) {
        monthlyCount = 0;
        dailyCount = 0;
        lastMonthKey = monthKey;
        lastDayKey = dayKey;
    } else if (lastDayKey !== dayKey) {
        dailyCount = 0;
        lastDayKey = dayKey;
    }

    return { ...summary, dailyCount, monthlyCount, lastDayKey, lastMonthKey };
}

// ─── Block reason strings ─────────────────────────────────────────────────────

function humanBlockReason(type: BlockType, expiresAt: Date): string {
    switch (type) {
        case "process_failed":
            return `Your previous border tax request failed during processing (document issue or government portal was unavailable). You can try again after ${expiresAt.toISOString()}.`;
        case "qr_generated":
            return `A QR code was generated for your previous border tax request but payment was not completed. You can try again after ${expiresAt.toISOString()}.`;
        case "paid_today":
            return `You have already paid your border tax today. You can request again after midnight (${expiresAt.toISOString()}).`;
        default:
            return `You are temporarily blocked until ${expiresAt.toISOString()}.`;
    }
}

// ─── Public: check eligibility (READ ONLY) ───────────────────────────────────

export async function checkBorderTaxEligibility(
    driverId: string,
): Promise<EligibilityResult> {

    // 1. Fetch membership
    const membershipActive = await getMembershipActive(driverId);
    if (membershipActive === null) {
        return { eligible: false, reason: "Driver not found." };
    }

    // 2. Fetch + rollover usage summary
    const raw = await readSummary(driverId);
    const summary = applyRollover(raw);
    const now = new Date();

    // 3. Check active block — any block type
    if (summary.blockType !== null && summary.blockExpiresAt !== null) {
        const expiresAt = summary.blockExpiresAt.toDate();
        if (now < expiresAt) {
            return {
                eligible: false,
                reason: humanBlockReason(summary.blockType, expiresAt),
                blockType: summary.blockType,
                unblockAt: expiresAt,
                membershipActive,
            };
        }
        // Block is expired — falls through as clean
    }

    // 4. Non-member path
    if (!membershipActive) {
        if (summary.totalAllTimePaid >= FREE_LIFETIME_LIMIT) {
            return {
                eligible: false,
                reason: "You have used your 1 free border tax. Please subscribe to a membership to continue.",
                membershipActive: false,
            };
        }
        return { eligible: true, membershipActive: false };
    }

    // 5. Member path — check monthly cap first, then daily
    if (summary.monthlyCount >= MONTHLY_LIMIT_MEMBERS) {
        return {
            eligible: false,
            reason: `You have reached the monthly limit of ${MONTHLY_LIMIT_MEMBERS} border taxes. Your limit resets next month.`,
            membershipActive: true,
        };
    }

    if (summary.dailyCount >= DAILY_LIMIT_MEMBERS) {
        const midnight = nextISTMidnight();
        return {
            eligible: false,
            reason: "You have already used your border tax for today. You can request again after midnight.",
            blockType: "paid_today",
            unblockAt: midnight,
            membershipActive: true,
        };
    }

    return { eligible: true, membershipActive: true };
}

// ─── Public: update usage after job completion ────────────────────────────────
//
// Called fire-and-forget from the job-completed handler in index.ts.
// This is the ONLY place that writes to driverUtilitiesRequests/borderTaxSummary usage.

export interface JobCompletionDetails {
    driverId: string;
    requestId: string;
    jobId: string;
    status: "done" | "failed" | "partial";
}

export async function updateBorderTaxUsageOnCompletion(
    details: JobCompletionDetails,
): Promise<{ ok: boolean; error?: string }> {
    const { driverId, requestId, jobId, status } = details;

    if (!driverId) return { ok: false, error: "driverId missing" };

    console.log(
        `[driverUsage] START driverId=${driverId} requestId=${requestId} ` +
        `jobId=${jobId} status=${status}`,
    );

    try {
        // Determine whether QR was generated — ground truth is qrCodeUrl on the request doc
        const reqSnap = await borderTaxRequestsRef.doc(requestId).get();
        const reqData = reqSnap.exists ? reqSnap.data() : null;
        const hasQR = !!(reqData?.qrCodeUrl);

        const ref = driverBorderTaxUsageRef(driverId);
        const raw = await readSummary(driverId);
        const rolled = applyRollover(raw);

        const dayKey = istDayKey();
        const monthKey = istMonthKey();
        const now = new Date();

        if (status === "done") {
            // Payment confirmed — increment all counters, block until next IST midnight
            const midnight = nextISTMidnight();

            await ref.set({
                dailyCount: rolled.dailyCount + 1,
                monthlyCount: rolled.monthlyCount + 1,
                totalAllTimePaid: rolled.totalAllTimePaid + 1,
                lastDayKey: dayKey,
                lastMonthKey: monthKey,
                // Explicit block until midnight prevents a second same-day request
                // even after counter rollover edge cases
                blockType: "paid_today",
                blockExpiresAt: Timestamp.fromDate(midnight),
                blockReason: "Paid today — resets at midnight IST.",
                blockedAt: Timestamp.fromDate(now),
                lastRequestId: requestId,
                updatedAt: FieldValue.serverTimestamp(),
            }, { merge: true });

            console.log(
                `[driverUsage] DONE: driverId=${driverId} ` +
                `daily=${rolled.dailyCount + 1} monthly=${rolled.monthlyCount + 1} ` +
                `allTime=${rolled.totalAllTimePaid + 1} ` +
                `blockedUntil=${midnight.toISOString()}`,
            );

        } else {
            // status = "failed" or "partial"
            // Determine block type from QR presence
            const blockType = hasQR ? "qr_generated" : "process_failed";
            const blockDurationMs = hasQR ? BLOCK_QR_GENERATED_MS : BLOCK_PROCESS_FAILED_MS;
            const expiresAt = new Date(now.getTime() + blockDurationMs);
            const blockReason = hasQR
                ? "QR code was generated but payment was not completed."
                : `Processing failed before QR code generation
                    (document issue or government portal unavailable)
                    Please make sure your PUCC, Insurance or Road Tax is Valid.`;

            // Do NOT touch counters — payment did not go through
            await ref.set({
                dailyCount: rolled.dailyCount,
                monthlyCount: rolled.monthlyCount,
                totalAllTimePaid: rolled.totalAllTimePaid,
                lastDayKey: dayKey,   // keep rollover sentinels fresh
                lastMonthKey: monthKey,
                blockType,
                blockExpiresAt: Timestamp.fromDate(expiresAt),
                blockReason,
                blockedAt: Timestamp.fromDate(now),
                lastRequestId: requestId,
                updatedAt: FieldValue.serverTimestamp(),
            }, { merge: true });

            console.log(
                `[driverUsage] BLOCKED: driverId=${driverId} ` +
                `type=${blockType} hasQR=${hasQR} ` +
                `until=${expiresAt.toISOString()}`,
            );
        }

        return { ok: true };

    } catch (e) {
        const msg = (e as Error).message;
        console.error(`[driverUsage] ERROR driverId=${driverId}:`, e);
        return { ok: false, error: msg };
    }
}
