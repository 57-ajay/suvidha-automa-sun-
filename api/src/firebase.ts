// api/src/firebase.ts
import { initializeApp, cert, type ServiceAccount } from "firebase-admin/app";
import { getFirestore } from "firebase-admin/firestore";
import { getStorage } from "firebase-admin/storage";

const serviceAccount = await Bun.file(`${import.meta.dir}/../service-account.json`).json() as ServiceAccount;

const app = initializeApp({
    credential: cert(serviceAccount),
    storageBucket: "bwi-cabswalle.appspot.com",
});

export const db = getFirestore(app);
export const storage = getStorage(app);

export const challanRequestsRef = db
    .collection("driverUtilitiesRequests")
    .doc("data")
    .collection("challanRequests");

export const borderTaxRequestsRef = db
    .collection("driverUtilitiesRequests")
    .doc("data")
    .collection("borderTaxRequests");

export function driverBorderTaxUsageRef(driverId: string) {
    return db
        .collection("driverUtilitiesRequests")
        .doc("borderTaxSummary")
        .collection(driverId)
        .doc("borderTaxUsage");
}

console.log("[FIREBASE] Initialized successfully (Firestore + Storage)");
