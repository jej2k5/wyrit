# Phase 1 handoff

The trusted host owns the capability registry in `src/wyrit/capabilities.py`. Assign IDs explicitly and keep them stable across description and schema edits. IDs use lowercase ASCII segments separated by dots, with each segment beginning with a letter. Registration rejects duplicate IDs.

Use `search` to retrieve pages of IDs and descriptions. Ranking favors exact ID matches, then distinct query term coverage, then coverage in the ID. Ties sort by ID. Queries with no terms or matches return an empty page. Call `describe` by exact ID for input and output schemas. The host uses `resolve` to access the full descriptor, including adapter metadata.

Schemas accept JSON objects and booleans. The model snapshots descriptor data and checks its JSON representation. JSON Schema dialect selection and payload validation belong at the invocation boundary. JSON serialization sorts descriptor keys and orders registry entries by ID. Keep adapter metadata free of secrets and use discovery projections for model-facing output.

The host constructs `RunAuthorization` with `authorization_for`, which rejects unknown capability IDs and snapshots the explicit grant set. A grant requires an exact run and capability ID match. Discovery never confers invocation authority. The invocation boundary must use the host-owned grants and enforce them before dispatch. This phase implements no runtime, dispatch, sandbox, or action lifecycle.

Run `python3 -m unittest discover -s tests` from the repository root.
