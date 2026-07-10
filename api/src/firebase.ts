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

// New challan flow (params.isNewChallanFlow === true): a TOP-LEVEL collection
// where each doc is a single challan (no challans[] array). The doc id is the
// subChallan id, which the API receives as params.requestId. receipt/aiAgentStatus
// are written at the doc top level. NOTE: challanNo is NOT unique in this
// collection, so never locate a doc by challanNo here — always use the doc id.
export const subChallanRequestsRef = db.collection("subChallanRequests");

export const borderTaxRequestsRef = db
    .collection("driverUtilitiesRequests")
    .doc("data")
    .collection("borderTaxRequests");

export function driverBorderTaxUsageRef(driverId: string) {
    return db
        .collection("driverUtilitiesRequests")
        .doc("borderTaxSummary")
        .collection("driverUsage")
        .doc(driverId);
}

console.log("[FIREBASE] Initialized successfully (Firestore + Storage)");
