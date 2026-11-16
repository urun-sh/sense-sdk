# Sense SDK contributor guidance

## Repository contracts

- This repository ships two packages: `urun-sense` from `python/` (import `sense`) and `@urun-sh/sense` from `typescript/`. The root Python project is a development workspace, not the published SDK.
- Keep Python and TypeScript behavior aligned. Shared fixtures in `conformance/fixtures/` describe results, predicates, and event handling; run both conformance runners when changing those contracts.
- Before changing wire messages or session lifecycle behavior, read [the wire protocol reference](docs/protocol.md). Keep it aligned with the implementation and document SDK differences explicitly.
- A wave is a complete set of answers to registered questions. Treat missing or null answers as protocol errors, never as negative answers.
- For binary questions, `p` means P(true), not confidence in the selected answer. For score questions, equality compares the top level name and ordering compares the expected level index.
- Preserve public API names and wire identifiers when editing terminology. Jev terminology, including `noul`, is intentional and should be retained. Public documentation and comments must make sense without private repositories, tickets, or company-specific shorthand.

## Local checks

- Python development uses `uv` and the root workspace lockfile. Use Python 3.11+ for the full suite; CI also tests the core SDK on 3.10, excluding the terminal demo that requires `Task.uncancel()`. TypeScript development uses Bun and Node.js 22 or newer. Run Python commands from the repository root and TypeScript commands from `typescript/`.
- Python tests, including optional transport coverage: `uv run --locked --project python --extra quic --extra webrtc pytest python/tests conformance/python_runner.py -q`. Without the transport extras, certificate-pinning and control-frame tests skip.
- Python lint: `uvx ruff@0.16.7 check .` from the repository root.
- TypeScript setup and build: `bun install --frozen-lockfile && npm run build`. Build before checking examples: they import the package through its generated `dist/` declarations.
- TypeScript checks: `bunx oxlint@1.91.0 && bunx tsc --noEmit && bun test && bun test ../conformance/typescript_runner.ts`.
- The offline tests use fixtures and local fake servers. Live examples send media to the configured service and can incur charges; run them only when explicitly requested, using credentials from the environment.
- See `.github/workflows/ci.yml` for duplication and clean-consumer checks. For packaging changes, test the built wheel or npm tarball outside the checkout so local source imports cannot mask missing files.

## Packaging and documentation

- Edit the source files at `examples/{python,typescript,assets}`, not `typescript/dist/` and not the staged copies: the Python build hook (`python/hatch_build.py`) stages the repo-root examples into `python/src/sense/examples/`, and `npm run build` (`typescript/scripts/stage-assets.mjs`) stages them into `typescript/examples/`. Both staged trees are gitignored.
- Keep the root README and both package READMEs consistent when changing public examples or requirements.
- Keep `LICENSE`, `python/LICENSE`, and `typescript/LICENSE` identical. Both published packages need their own license copy.
- For a release, keep the Python and TypeScript package versions and applicable lockfiles consistent. The `v*` tag workflow publishes to PyPI and npm; creating or pushing a release tag is a publishing action.
