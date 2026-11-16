// Stage the repo-root example sources into examples/ so the npm tarball
// carries them: the committed source lives at ../examples/{typescript,assets}
// (one level up, the repo root) and every build copies it into the package
// tree. Node APIs, not shell commands, so the build runs on Windows too.
// package.json's files negation keeps the staged test out of the tarball;
// bun test still runs it from here.
import { copyFileSync, cpSync, mkdirSync, rmSync } from "node:fs";

rmSync("examples", { recursive: true, force: true });
cpSync("../examples/typescript", "examples", { recursive: true });
mkdirSync("examples/assets", { recursive: true });
copyFileSync(
  "../examples/assets/mic-question-48k-mono.wav",
  "examples/assets/mic-question-48k-mono.wav",
);