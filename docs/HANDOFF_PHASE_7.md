# Phase 7 handoff and operator quickstart

Wyrit exposes two agent-facing MCP tools over stdio. `search` ranks matches within an operator-configured capability set and returns up to five public descriptors with input and output schemas and JavaScript usage. `execute` accepts a JavaScript async function body, creates a new run ID and grant, and delegates to the disposable native-sandboxed runtime and trusted host.

## Run the demo

Use a trusted Node installation supporting the runtime's `--permission`, `--no-allow-net` and `--experimental-vm-modules` flags. The host must support a native sandbox described in [the Phase 6 handoff](HANDOFF_PHASE_6.md).

From the repository root, install Wyrit in the Python environment used by your MCP client:

```sh
python3 -m pip install .
```

Configure the MCP client to spawn the demo with that environment's Python executable. A typical stdio configuration is:

```json
{
  "mcpServers": {
    "wyrit": {
      "command": "/absolute/path/to/python",
      "args": ["-m", "wyrit.mcp_server"]
    }
  }
}
```

The child needs a `PATH` containing the trusted Node executable. Call `search` with `{"query":"integer value"}` to discover `demo.increment` and `demo.format`. Call `execute` with the following function body in its `source` argument:

```js
const row = await tools.call("demo.increment", {value: 41});
return await tools.call("demo.format", row);
```

Expect `status: "SUCCESS"`, `value: "value=42"`, and two ordered `SUCCEEDED` actions. The response also has a host-generated `run_id`, nullable `reason`, `unknown_calls`, and guest cancellation flags. MCP SDK versions with structured tool output can expose the object in `structuredContent`. With MCP SDK 1.8, parse the JSON text in the result's content block. A completed MCP call can carry a failed Wyrit run, so inspect `status` and `actions` in the returned object.

## Connect host capabilities

In the trusted host, construct a `CapabilityRegistry` and pass it with a dispatcher and an explicit `allowed_capability_ids` iterable to `wyrit.mcp_server.create_server`. For a downstream MCP service, initialize its session and portal in the host, then use `MCPAdapter.capabilities` and `MCPAdapter.dispatch` as the registry entries and dispatcher. Keep the session, portal, credentials, and any authentication configuration in the trusted host. Launch the returned server with `.run(transport="stdio")`.

Each server instance uses one operator-selected trust scope for all its callers. Each execute request gets a fresh authorization bound to that run and uses the runtime's two-second deadline. Scope the instance to the MCP client authorized to use those capabilities. A denied call never reaches the dispatcher and has no external action record. If an action is `UNKNOWN`, check the underlying system before deciding whether to submit another run. Wyrit performs no automatic replay.

## Release verification

```sh
python3 -m unittest discover -s tests
WYRIT_REQUIRE_NATIVE_SANDBOX=1 python3 -m unittest discover -s tests
```

Run the second command on the release host to enforce native sandbox coverage. The Phase 7 suite exercises the MCP surface, including a real downstream stdio MCP session, two-capability composition, tool error, out-of-scope capability denial, timeout after dispatch with an unknown outcome, malformed code, final output limits with a preserved action outcome, and unavailable sandbox enforcement. The sandbox-unavailable test verifies that the server returns a structured failure with no launch or dispatch.

The implementation environment refused nested Seatbelt activation. The ordinary suite passed with native-dependent cases skipped. Native composition and containment still require the release-host check. Linux containment retains the verification status documented in the Phase 6 handoff.
