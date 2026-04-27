// Mock handlers — replace with Firestore writes later

interface InternalRequest {
    jobId: string;
    params: Record<string, string>;
    data: unknown;
}

export function handleSaveChallans(body: InternalRequest) {
    const challans = Array.isArray(body.data) ? body.data : [];
    const vehicle = body.params?.vehicleNumber ?? "unknown";

    console.log(
        `[MOCK] save_challans | job=${body.jobId} vehicle=${vehicle} count=${challans.length}`
    );
    for (const c of challans) {
        console.log(`  → ${c.challanId} | ${c.offence} | ₹${c.amount} | ${c.date}`);
    }

    return { ok: true, saved: challans.length, vehicle };
}

export function handleSaveDiscounts(body: InternalRequest) {
    const discounts = Array.isArray(body.data) ? body.data : [];
    const vehicle = body.params?.vehicleNumber ?? "unknown";

    console.log(
        `[MOCK] save_discounts | job=${body.jobId} vehicle=${vehicle} count=${discounts.length}`
    );
    for (const d of discounts) {
        console.log(
            `  → ${d.challanId} | discount=₹${d.discountAmount} | original=₹${d.originalAmount}`
        );
    }

    return { ok: true, saved: discounts.length, vehicle };
}
