# Phase 8 handoff

Wyrit requires Python 3.11 or newer. Copy [wyrit.example.toml](../wyrit.example.toml) to `wyrit.toml`, configure trusted downstream servers, and start Wyrit with `python -m wyrit.mcp_server --config wyrit.toml`. Start the two built-in capabilities with `python -m wyrit.mcp_server --demo`.

Configuration uses named `[servers.<name>]` tables. Each needs a unique `namespace`, a `transport`, and `expose`. Set `expose = "all"` to include every discovered tool, or list exact downstream tool names. An empty list grants no tools. Stdio uses `command` and optional `args`. HTTP uses a Streamable HTTP MCP endpoint at `url` and optional `headers` referencing nonempty environment variables. Set the referenced environment variables before launch and use HTTPS for remote authenticated endpoints.

`wyrit.host_config.load_config(path)` returns an immutable `HostConfig`. It reads TOML and validates the full configuration before any downstream connection is opened. `wyrit.configured_host.configured_capabilities(config)` owns the downstream sessions for the lifetime of the MCP server. Startup stops if any downstream connection or discovery fails, including when a configured tool is absent. The discovered, exposed capability IDs form each run's grant. Existing sandbox, dispatch deadline, and action outcome behavior still apply.

Resolved header values stay in the trusted host. The configured CLI suppresses raw SDK diagnostics and discards downstream stdio stderr. Discovery refuses exposed descriptors containing a configured secret, and dispatch rejects adapted results containing one. The HTTP client disables redirects and environment-derived proxy settings.

Run `python -m unittest discover -s tests` from the repository root. Phase coverage includes the CLI startup modes, fail-closed validation, search through a real stdio subprocess, and HTTP credential and redirect handling through a mocked HTTPX transport. Native sandbox-dependent tests require a supported release host.
