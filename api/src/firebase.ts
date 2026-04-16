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

console.log("[FIREBASE] Initialized successfully (Firestore + Storage)");
