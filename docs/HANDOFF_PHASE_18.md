# Phase 18 handoff

`wyrit.embed.Host` accepts a static capability list or a per-run catalog provider, async dispatch, and an optional audit sink. See [Embedding](EMBEDDING.md) for the API and run context contract. [The example](../examples/embed/main.py) runs one script for two tenants and shows a host policy denial.

Embedded execution and MCP execute and recipe tools use the same Host execution path. The configured MCP binding pins the downstream catalog and dispatch together, preserving reconnect checks, operator write policy, the downstream send boundary, and source capture. Embedded dispatch uses the read-only policy and its own run budget accounting. The sandbox protocol retains the same process boundary and carries a dispatch-supplied public reason when present.

Use a host with a supported native sandbox to run the full integration gate and example.

```sh
python -m pip install -e .
WYRIT_REQUIRE_NATIVE_SANDBOX=1 python -m unittest discover -s tests
python examples/embed/main.py
```

The phase workspace denied nested macOS sandbox activation. The ordinary suite reported native sandbox skips. The native-independent tests exercised timeout handling and fail-closed admission. A separate process-local harness used a test launcher to check the embedded JavaScript protocol and example output. Use the commands above for native end-to-end verification on a supported host.
