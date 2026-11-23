# Phase 12 handoff

Start the console with `python -m wyrit.console --config wyrit.toml`. Open `http://127.0.0.1:4790` or pass `--port N` to choose another port. The CLI owns its configured host. An independently launched MCP process owns a separate host and needs its own reload.

- **Servers** manages downstream transports and shows connection health, discovered tools, and whether each header environment variable is set. **Test connection** initializes a temporary host-owned session and discovers tools before you save. Testing a stdio server starts its configured executable. HTTP credential headers require HTTPS. A disabled server can retain its environment references while the variables are missing.
- **Capabilities** searches discovered descriptors, shows input and output schemas and read/write classification, and controls exposure. Hiding a tool records its name in `exclude` so automatic exposure still applies to newly discovered tools and the hidden tool keeps its write policy. A disabled server has no live discovery session.
- **Policy and limits** changes server and capability write allowances and displays each execution limit's effective value and source. Clear an override to inherit the global setting or built-in default.

Hand edits use the same TOML and validator. Use **Reload file** to apply them to the console host. Console edits validate a complete candidate before replacing the file, keep the previous contents in `wyrit.toml.bak`, and call the Phase 9 reload API. An invalid candidate leaves the file, backup, and host unchanged. A successful console edit rewrites TOML in canonical form, including the quoted keys. The backup retains the previous file's comments and formatting.

The threaded HTTP server binds to IPv4 loopback, requires the matching Host header, and checks the page's per-launch token on configuration and probe requests. The nonce-scoped page and bundled JetBrains Mono require no external assets. Header values resolve in the trusted host. The console suppresses request and downstream SDK logging.

Run `python3 -m unittest discover -s tests`. The offline console tests exercise raw HTTP requests and responses in memory and use a local stdio fixture for discovery. They cover Host and token checks, CSP and assets, TOML equivalence, the secret boundary, hand reload, and invalid-change isolation. No browser or listening socket is required for the tests.
