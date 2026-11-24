# Wyrit quickstart

## Run standalone

From the repository root, install Wyrit with Python 3.11 or later. Put Node.js on PATH for guest execution. On Linux, follow the [native sandbox verification handoff](HANDOFF_PHASE_19.md) before enabling guest execution.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
cp wyrit.example.toml wyrit.toml
```

For a first downstream server, save this as `hello_mcp.py` next to `wyrit.toml`. It uses the MCP SDK installed with Wyrit.

```python
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server = FastMCP("hello")

@server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def greet(name: str) -> dict[str, str]:
    return {"greeting": f"Hello, {name}!"}

if __name__ == "__main__":
    server.run()
```

Append this to `wyrit.toml` and replace both paths with absolute paths in your checkout.

```toml
[servers.hello]
namespace = "hello"
transport = "stdio"
command = "/path/to/wyrit/.venv/bin/python"
args = ["/path/to/wyrit/hello_mcp.py"]
expose = ["greet"]
```

Start the console with `python -m wyrit.console --config wyrit.toml` and open `http://127.0.0.1:4790`. Check the server under **Servers** and look for `hello.greet` under **Capabilities**.

Configure your MCP client to launch the virtual environment's Python as a stdio server with arguments `-m wyrit.mcp_server --config /path/to/wyrit/wyrit.toml`. Call Wyrit's `search` tool with `{"query":"greet"}`, then pass this JavaScript as the `source` argument to `execute`.

```javascript
return await tools.call("hello.greet", {name: "Ada"});
```

The run returns a greeting and records a `SUCCEEDED` action. The console and MCP process each load their own host. After editing the configuration, reload the console and restart the MCP process.

## Optionally pair with MCPxy

With MCPxy running and a GitHub upstream configured, uncomment the MCPxy block in `wyrit.example.toml` or add it to your existing Wyrit config.

```toml
[servers.mcpxy]
namespace = "github"
transport = "http"
url = "https://proxy.example.com/mcp/github"
expose = ["list_issues"]

[servers.mcpxy.headers]
Authorization = { env = "MCPXY_PROXY_TOKEN", prefix = "Bearer " }
```

Use your MCPxy HTTPS address and upstream slug in `/mcp/<upstream>`. Wyrit requires HTTPS for credential headers. Supply `MCPXY_PROXY_TOKEN` from your secret manager in the environment of each trusted Wyrit process before launch. MCPxy holds the upstream credentials and sees requests attributed to the proxy credential. Wyrit resolves the bearer header in the host and runs guest JavaScript with an empty process environment.

Check discovery in the console, then use `search` to get the tool ID and input schema before calling it with `execute`. Adjust `expose` to the tool names your upstream advertises. If the selected read tool lacks read-only annotations, verify its behavior and set `classification = "read"` in its per-tool configuration. Wyrit denies writes by default.

See the [phase handoff](HANDOFF_PHASE_20.md) for the live operator check.
