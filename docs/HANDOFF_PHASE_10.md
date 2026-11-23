# Phase 10 handoff

Configure `[limits]` for the host and `[servers.<name>.limits]` for individual downstream servers. An omitted server value inherits the host value, and an omitted host value uses the built-in default. Execute returns `limits.global` and `limits.servers` with the value and source of each limit for the run's authorized server namespaces. A configured host snapshots these settings and the ledger path when it binds a run. Reloading budget settings alone reuses downstream connections.

| Setting | Default | Applies to |
| --- | ---: | --- |
| `run_deadline` | 10 seconds | Elapsed time since runtime startup |
| `call_timeout` | 5 seconds | Each capability dispatch, including downstream wait |
| `max_calls` | 32 | Total valid call requests in a run, including denied requests |
| `max_output_bytes` | 16,384 bytes | Each capability response and the final return value, measured as compact ASCII JSON |

A run starts with the global deadline and call count limit. The first authorized request to a configured server selects that server's effective run deadline and call count limit. The deadline still measures from runtime startup, and all previous call requests count. If the run reaches another server, the stricter effective value for each of those limits governs the rest of the run. Each capability response uses its own server's effective byte limit. The final return value uses the global byte limit. Source and protocol framing have separate fixed safety ceilings.

A terminal limit hit returns a structured `limit_hit` with its name, value, source, and server namespace when applicable. A run deadline yields `TIMED_OUT`. Call count and output violations yield `FAILED`. A call timeout rejects that call in JavaScript and lets the script continue or catch the error. Once the host hands the request to the transport, a call timeout leaves the action `UNKNOWN`. The host does not replay the request. An oversized result can end the run with the external action still recorded as `SUCCEEDED`. If no terminal limit wins, `limit_hit` reports the first call timeout.

The run ledger defaults to `$XDG_STATE_HOME/wyrit/runs.jsonl` when the variable holds an absolute path and to `~/.local/state/wyrit/runs.jsonl` otherwise. Set the top-level `ledger_path` to override it. Relative paths resolve against the TOML file. The trusted host appends one JSON object per run, even for early failures, using mode `0600`. A ledger open failure prevents launch. An append failure surfaces through `ledger_error` and preserves the returned run and action outcomes.

```python
from wyrit.ledger import read_ledger

for record in read_ledger():  # Pass the configured path when overriding the default.
    print(record["run_id"], record["outcome"])
```

Records contain the run ID, Unix start and end timestamps in seconds, run outcome, structured limit hit, and per-action metadata. An action contains its capability ID, outcome, elapsed seconds, and result size in bytes when known. Denied requests with untrusted capability names use a null capability field. Code, arguments, results, and exception text are excluded.

If cancellation or the run deadline interrupts a call before send, the ledger records the attempt as `FAILED` and the returned actions still include earlier dispatched calls and their outcomes. Unexpected runtime failures also preserve finalized actions and append a failure record.

Downstream servers with credential headers must use HTTPS. Plain HTTP remains available for servers without credential headers.

Run `python3 -m unittest discover -s tests` to verify. The budget tests use a fixed protocol peer to exercise the trusted controller even where nested sandbox setup is unavailable. The native integration tests additionally exercise the JavaScript timeout rejection and output limits through the real sandbox when supported.
