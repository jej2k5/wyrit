# Phase 8: Host configuration

Wyrit starts from a config file instead of code: `python -m wyrit.mcp_server --config wyrit.toml`. Without `--config` it refuses to start and names the flag; the two demo capabilities move behind `--demo`. Set `requires-python = ">=3.11"` in `pyproject.toml` and read the file with the standard library's `tomllib`; add no dependency.

The file declares downstream MCP servers. Each has a namespace (used as its capability prefix), a transport (`stdio` with a command and args, or `http` with a URL), optional headers whose values reference environment variables by name (for example `Authorization = { env = "GITHUB_TOKEN", prefix = "Bearer " }`), and an exposure rule (all tools, or an explicit list). Secrets never appear inline: a literal header value that looks like a credential is a config error.

Validation fails closed with a message that names the server and field: unknown keys, duplicate namespaces, a missing environment variable, a malformed URL, an empty command. Validation is a separate pure function (`load_config(path) -> HostConfig`) that later phases and the console reuse. Ship `wyrit.example.toml` at the repository root with a commented example.

Tests: a config pointing at a local fake stdio MCP server exposes its tools through `search`; every validation failure above refuses startup with its message; a secret value never appears in logs, errors or tool results. Produce the phase handoff.


**Test secrets (operator note, 2026-11-23):** buildctl refuses any changed file containing a credential-shaped literal (`ghp_…`, `sk-…`, JWT-shaped strings, or `token = "<long literal>"`-style assignments), even a fake one, and fails the phase. Build fake secrets at runtime instead, for example `FAKE_TOKEN = "ghp_" + "x" * 36` or `"s" + "k-" + "test" * 6`, and assert on that variable; never write the assembled value into a source, fixture or snapshot file. Phase 8 attempt 1 failed this way in `tests/test_host_config.py`.
