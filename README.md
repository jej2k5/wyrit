# Wyrit

**Wire your tools. Run the work.**

Wyrit is an open-source, local-first capability execution layer for AI agents. Agents discover relevant capabilities, compose them in JavaScript, and execute that code inside a restricted disposable runtime. Credentials, authorization, upstream MCP/API/data connections, and execution accounting remain in the trusted Wyrit host.

## Architecture

```text
Agent <-> MCP: search / execute <-> Wyrit Host <-> MCP / API / data
                                  ^
                                  | bounded invocation protocol
                                  v
                         Disposable JS runtime
```

The runtime receives no ambient credentials, network, filesystem, shell/process, or package-loading authority. Platform-specific sandboxing reinforces this contract and must fail closed when a promised guarantee cannot be enforced.

The architecture and build roadmap are grounded in source inspection plus REA investigation of Cloudflare Code Mode. See `docs/research/` and `docs/REFERENCE_ARCHITECTURE.md`.

## Status

Pre-implementation. The repository is intentionally governed by buildctl before product code is written.
