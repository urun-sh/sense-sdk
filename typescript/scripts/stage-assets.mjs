// Stage the bundled sample wav into examples/assets/ so the npm tarball
// carries it: the single committed copy lives in the Python package tree.
// Node APIs, not shell commands, so the build runs on Windows too.
import { copyFileSync, mkdirSync } from "node:fs";

mkdirSync("examples/assets", { recursive: true });
copyFileSync(
  "../python/src/sense/examples/assets/mic-question-48k-mono.wav",
  "examples/assets/mic-question-48k-mono.wav",
);
