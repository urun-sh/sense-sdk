# Contributing

For bugs and feature requests, open a GitHub issue with the package version,
runtime, expected result, and a small reproduction using synthetic inputs.
For security reports, follow [SECURITY.md](SECURITY.md) instead. Be respectful
and keep discussions focused on the code. Support is provided through issues
on a best-effort basis.

## Development

Use Python 3.11+ for the full test suite, uv, Bun, and Node.js 22+. The core SDK
also supports Python 3.10; CI tests it separately from the terminal demo,
whose cancellation cleanup requires Python 3.11+. The root `uv.lock` is authoritative
for the Python workspace, including commands run with `--project python`.
Do not generate a separate lockfile in `python/`.

From the repository root:

```sh
uv sync --locked --project python --extra quic --extra webrtc
uv run --locked --project python --extra quic --extra webrtc pytest python/tests conformance/python_runner.py -q
uvx ruff@0.16.7 check .
```

From `typescript/`:

```sh
bun install --frozen-lockfile
npm run build
bunx oxlint@1.91.0
bunx tsc --noEmit
bun test
bun test ../conformance/typescript_runner.ts
```

Tests use fixtures and local fake servers. Running a live example sends media
to the configured service and may incur charges; it is not needed for an
ordinary contribution. Never commit credentials, recordings of other people,
or private infrastructure details.

## Pull requests

Keep changes focused, describe any behavior or compatibility changes, and
include the checks you ran. Preserve Jev terminology and public API names.
For wire or lifecycle changes, consult [the protocol reference](docs/protocol.md)
and test both language implementations. Document intentional differences.

Build outputs are generated, not edited. The sample WAV has one tracked copy
at `examples/assets/`; both package builds stage it. Packaging changes must
pass the clean-consumer checks in CI. Keep all three license copies identical.
The release workflow publishes on `v*` tags; do not create release tags as part
of a contribution.
