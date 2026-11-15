// Post-build fixup: rewriteRelativeImportExtensions rewrites .ts -> .js in the
// emitted .js but NOT in the emitted .d.ts (tsgo 7.0.2 and tsc 5.9 both leave
// declaration imports pointing at "./x.ts"). A dist consumer without
// allowImportingTsExtensions cannot resolve those. Rewrite declaration
// specifiers to .js so the built package is consumable from a clean install.
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const dist = join(dirname(fileURLToPath(import.meta.url)), "..", "dist");
let fixed = 0;
for (const f of readdirSync(dist)) {
  if (!f.endsWith(".d.ts")) continue;
  const p = join(dist, f);
  const before = readFileSync(p, "utf8");
  const after = before.replace(/\.ts"/g, '.js"');
  if (after !== before) {
    writeFileSync(p, after);
    fixed++;
  }
}
if (fixed === 0) {
  console.error("fix-dts: no .d.ts needed rewriting — remove this step if the compiler ever fixes declaration emit");
  process.exit(1);
}
console.log(`fix-dts: rewrote .ts -> .js specifiers in ${fixed} declaration file(s)`);
