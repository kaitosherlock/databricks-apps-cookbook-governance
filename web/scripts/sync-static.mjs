/**
 * Copy the exported site into the Python app so one process can serve it.
 *
 * Next.js writes a static export to ./out. Databricks Apps deploys the
 * governance-app/ directory only, and cannot run npm at deploy time, so the
 * build output has to live inside that directory and be committed.
 */
import { cp, rm, mkdir, stat } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = resolve(here, "..", "out");
const destination = resolve(here, "..", "..", "governance-app", "static");

try {
  await stat(source);
} catch {
  console.error(`No export found at ${source}. Run \`next build\` first.`);
  process.exit(1);
}

await rm(destination, { recursive: true, force: true });
await mkdir(destination, { recursive: true });
await cp(source, destination, { recursive: true });

console.log(`Static export copied to ${destination}`);
console.log("Commit that directory, then Deploy the Databricks App.");
