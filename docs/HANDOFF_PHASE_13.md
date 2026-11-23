# Phase 13 handoff

Open **Monitoring** in the loopback console. To view an agent host’s runs, start the console with the same configured ledger path as that host. The health strip describes the live console supervisor’s own connections. The server history shows recorded events from all supervisors sharing that path and identifies each supervisor.

- The feed shows completed runs in newest-first order with calls in their original order. Pause freezes new feed entries and resume catches up from the same cursor. A downstream check is suggested for UNKNOWN.
- Capability rates use named call attempts in the selected window. p50 and p95 use nearest rank over recorded durations. The health strip counts runs completed in the last hour and flags increases in failed or denied action rates against the preceding 23 hours when baseline calls exist.
- Server history, reconnects, calls lost to connection drops, limit hits and write-policy denials use the selected window. Write denials and drops require an explicit audit reason. Older ledger rows still contribute to outcome rates and can show the single limit recorded at the time.
- Source links return the exact JSON record with its file name, inode and line number. The initial feed shows the latest 50 records available in the current file. Once an inode is observed, the tail keeps its read position and follows late appends across rename rotation. Open rotation segments and scanned records stay in memory for the console session. Rotation performed entirely between polls can leave an intermediate file unobserved.
- Capture is off by default. The operator sets retention per named server and namespace under **Payload capture**. Captured arguments, available results and generated source are kept in a separate private SQLite sidecar. Known resolved credentials and common credential-shaped strings suppress the entire capture. Disabling capture purges that server’s retained payloads. Expired rows become unavailable at their deadline and a running supervisor sweeps them once a second, including paths seen before a live reload. Existing expired rows are swept when a supervisor next opens the same ledger path. SQLite secure deletion applies to retained rows. Filesystem snapshots and backups have their own retention.
- The console writes bare TOML keys where possible and uses inline header references. Header rows show whether their environment variables are set and preserve the config validator and atomic edit flow.

The trusted host still owns session access, the run authorization check and transport dispatch. Monitoring records and captures remain in the host process and are unavailable through the generated runtime. The public action outcome codes and replay behavior are unchanged.

## Verification

Run `python3 -m unittest discover -s tests` and `node --check src/wyrit/console/assets/console.js`. Offline tests exercise hand-computed windows and percentiles, explicit drop and denial records, rename and truncation rotation, capture opt-in and scheduled expiry, and console config serialization and source links. Native sandbox coverage depends on the test platform.
