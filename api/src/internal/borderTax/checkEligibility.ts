// api/src/routes/borderTax/checkEligibility.ts
//
// POST /api/border-tax/check-eligibility
//
// Called by the mobile app / frontend BEFORE initiating a border tax
// request. Returns whether the driver is currently allowed to start one.
//
// Request body:
//   { driverId: string }
//
// Response (200):
//   {
//     eligible: true,
//     membershipActive: boolean
//   }
//
// Response (200, ineligible):
//   {
//     eligible: false,
//     reason: string,           // human-readable, safe to show to driver
//     blockType?: string,       // "qr_generated" | "process_failed" | "paid_today"
//     unblockAt?: string,       // ISO 8601 UTC
//     membershipActive?: boolean
//   }
//
// Response (400): missing/invalid driverId
// Response (404): driver not found in Firestore
// Response (500): unexpected server error

import { checkBorderTaxEligibility } from "../../internal/borderTax/driverUsage";

export async function handleCheckEligibility(req: Request): Promise<Response> {
    let body: Record<string, any>;

    try {
        body = await req.json() as { driverId: string };
    } catch {
        return Response.json(
            { ok: false, error: "Request body must be valid JSON." },
            { status: 400 },
        );
    }

    const driverId: string | undefined =
        typeof body?.driverId === "string" ? body.driverId.trim() : undefined;

    if (!driverId) {
        return Response.json(
            { ok: false, error: "driverId is required." },
            { status: 400 },
        );
    }

    console.log(`[checkEligibility] START driverId=${driverId}`);

    try {
        const result = await checkBorderTaxEligibility(driverId);

        // Driver not found
        if (!result.eligible && result.reason === "Driver not found.") {
            console.log(`[checkEligibility] driverId=${driverId} → not found`);
            return Response.json(
                { ok: false, error: "Driver not found." },
                { status: 404 },
            );
        }

        console.log(
            `[checkEligibility] driverId=${driverId} ` +
            `eligible=${result.eligible} ` +
            `reason=${result.reason ?? "—"} ` +
            `blockType=${result.blockType ?? "—"} ` +
            `membershipActive=${result.membershipActive}`,
        );

        return Response.json({
            eligible: result.eligible,
            membershipActive: result.membershipActive,
            ...(result.eligible
                ? {}
                : {
                    reason: result.reason,
                    ...(result.blockType ? { blockType: result.blockType } : {}),
                    ...(result.unblockAt ? { unblockAt: result.unblockAt.toISOString() } : {}),
                }
            ),
        });

    } catch (e: any) {
        console.error(`[checkEligibility] ERROR driverId=${driverId}:`, e);
        return Response.json(
            { ok: false, error: "Internal server error." },
            { status: 500 },
        );
    }
}
