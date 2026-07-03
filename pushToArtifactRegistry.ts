// FILE: ~/projects/suvidha-automa-sun-/pushToArtifactRegistry.ts
//
// Build the agent images (api, worker) from THIS repo and push them to Artifact Registry.
// Runs from anywhere — it always operates on the repo this file lives in.
//
//   bun pushToArtifactRegistry.ts                 # build + push everything
//   bun pushToArtifactRegistry.ts --only worker   # just one image
//   bun pushToArtifactRegistry.ts --tag hotfix1   # extra tag on top of latest/ts/sha
//   bun pushToArtifactRegistry.ts --dry-run       # print what would run, do nothing
//
// Every image is pushed as:
//   :latest            ← what the nightly VM pulls
//   :YYYYMMDD-HHMM     ← rollback point
//   :<git short sha>   ← trace an image back to a commit (skipped if not a git repo)
//
// Rollback = re-point latest at an old tag:
//   gcloud artifacts docker tags add <base>/worker:20260702-2110 <base>/worker:latest
//
// NOTE on credentials: api/service-account.json is baked INTO the api image (Firestore
// auth). Vertex AI auth does NOT come from any file — it comes from the VM's service
// account + cloud-platform scope, which the backend sets when it creates the VM.

import { $ } from "bun";

// ─── Config ───────────────────────────────────────────────────────────────────
const PROJECT = "cabswale-ai";          // compute project (where AR + VMs live)
const REGION = "asia-south1";
const REPO = "challan-agent";
const REGISTRY_HOST = `${REGION}-docker.pkg.dev`;
const BASE = `${REGISTRY_HOST}/${PROJECT}/${REPO}`;

// name → docker build context (relative to repo root). Add future images here.
const IMAGES: Record<string, string> = {
    api: "./api",
    worker: "./worker",
};

// ─── Args ─────────────────────────────────────────────────────────────────────
const argv = process.argv.slice(2);
const DRY = argv.includes("--dry-run");
const extraTag = argv.includes("--tag") ? argv[argv.indexOf("--tag") + 1] : null;
const only = argv.includes("--only")
    ? (argv[argv.indexOf("--only") + 1] || "").split(",").map((s) => s.trim()).filter(Boolean)
    : null;

const targets = Object.entries(IMAGES).filter(([name]) => !only || only.includes(name));
if (targets.length === 0) {
    console.error(`--only matched nothing. Known images: ${Object.keys(IMAGES).join(", ")}`);
    process.exit(1);
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
const ROOT = import.meta.dir;   // the repo root = where this file lives
$.cwd(ROOT);

function ts(): string {
    const d = new Date();
    const p = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
}

async function run(parts: string[], describe: string) {
    console.log(`  $ ${parts.join(" ")}`);
    if (DRY) return;
    try {
        await $`${parts}`;
    } catch (e: any) {
        console.error(`\nFAILED: ${describe}`);
        console.error(e?.stderr?.toString?.() || e?.message || e);
        process.exit(1);
    }
}

// ─── Main ─────────────────────────────────────────────────────────────────────
console.log(`push-to-artifact-registry  →  ${BASE}   ${DRY ? "[DRY RUN]" : ""}`);
console.log(`repo root: ${ROOT}\n`);

// Preflight
const fs = await import("node:fs");
for (const [name, ctx] of targets) {
    if (!fs.existsSync(`${ROOT}/${ctx}/Dockerfile`)) {
        console.error(`ERROR: ${ctx}/Dockerfile not found — is this the agent repo?`);
        process.exit(1);
    }
}
if (targets.some(([n]) => n === "api") && !fs.existsSync(`${ROOT}/api/service-account.json`)) {
    console.error("ERROR: api/service-account.json missing — it must be baked into the api image.");
    process.exit(1);
}

// docker: use sudo only if plain docker isn't usable (temp-vm needs sudo)
let DOCKER = ["docker"];
try {
    await $`docker info`.quiet();
} catch {
    DOCKER = ["sudo", "docker"];
    console.log("(plain `docker` not usable — using `sudo docker`)\n");
}

// gcloud auth
let token = "";
try {
    token = (await $`gcloud auth print-access-token`.quiet().text()).trim();
} catch {
    console.error("ERROR: gcloud is not authenticated. Run: gcloud auth login");
    process.exit(1);
}

// git sha (optional)
let sha = "";
try {
    sha = (await $`git rev-parse --short HEAD`.quiet().text()).trim();
} catch {
    console.log("(not a git checkout — skipping git-sha tag)");
}

const TS = ts();
const tagsFor = (name: string) => {
    const tags = [`${BASE}/${name}:latest`, `${BASE}/${name}:${TS}`];
    if (sha) tags.push(`${BASE}/${name}:${sha}`);
    if (extraTag) tags.push(`${BASE}/${name}:${extraTag}`);
    return tags;
};

// Ensure the AR repo exists (idempotent)
console.log("Ensuring Artifact Registry repo exists…");
if (!DRY) {
    try {
        await $`gcloud artifacts repositories describe ${REPO} --location=${REGION} --project=${PROJECT}`.quiet();
        console.log("  (exists)");
    } catch {
        await run(
            ["gcloud", "artifacts", "repositories", "create", REPO,
                "--repository-format=docker", `--location=${REGION}`, `--project=${PROJECT}`,
                "--description=challan settlement agent images"],
            "create Artifact Registry repo",
        );
    }
} else {
    console.log("  (dry run — skipped)");
}

// Docker login (token via stdin — never lands in argv or shell history)
console.log(`Logging docker into ${REGISTRY_HOST}…`);
if (!DRY) {
    try {
        await $`${DOCKER} login -u oauth2accesstoken --password-stdin https://${REGISTRY_HOST} < ${new Response(token)}`.quiet();
    } catch (e: any) {
        console.error("FAILED: docker login");
        console.error(e?.stderr?.toString?.() || e?.message || e);
        process.exit(1);
    }
}

// Build + push
for (const [name, ctx] of targets) {
    const tags = tagsFor(name);
    console.log(`\n── ${name}  (context ${ctx}) ──`);
    await run([...DOCKER, "build", ...tags.flatMap((t) => ["-t", t]), ctx], `build ${name}`);
    for (const t of tags) {
        await run([...DOCKER, "push", t], `push ${t}`);
    }
}

if (!DRY) await $`${DOCKER} image prune -f`.quiet().nothrow();

// Summary
console.log(`\n${"═".repeat(68)}`);
console.log(`Pushed${DRY ? " (dry run — nothing actually pushed)" : ""}:`);
for (const [name] of targets) {
    console.log(`  ${BASE}/${name}:latest   (+ :${TS}${sha ? ` :${sha}` : ""}${extraTag ? ` :${extraTag}` : ""})`);
}
console.log(`\nThe next nightly VM pulls :latest automatically — this WAS the deploy.`);
console.log(`Rollback example:`);
console.log(`  gcloud artifacts docker tags add ${BASE}/worker:${TS} ${BASE}/worker:latest`);
console.log("═".repeat(68));
