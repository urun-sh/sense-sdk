# Security

Do not post API keys, customer media, or vulnerability details in public issues
or pull requests. Revoke any exposed credential immediately; deleting a file
does not remove it from Git history.

To report a suspected vulnerability, use [uRun's contact page](https://urun.sh/contact)
to request a private security-reporting channel. Initially include only the
affected package/version and your contact details, not exploit details or
sensitive data. If GitHub's private "Report a vulnerability" option is available
on this repository's Security tab, you may use it instead.

Include a minimal reproduction, impact, runtime version, and package version in
the private report. Use synthetic inputs and redact credentials. Do not test
against other users' data or accounts.

Check the latest published package before reporting a dependency vulnerability
and include the advisory identifier. This project does not currently promise
security backports for older releases or a response-time SLA.

The browser demo relays audio using the operator's configured API key. It
listens on this machine only unless started with `--lan`, and its relay accepts
only same-origin pages that carry the per-run access token printed at startup.
Each run allows a fixed number of sessions. With `--lan`, anyone who has the
printed URL can use the key; run it only on a trusted network and stop it after
testing. Direct browser SDK use is not supported; keep long-lived API keys on
the server.
