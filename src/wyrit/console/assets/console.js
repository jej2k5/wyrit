"use strict";

const page = document.body.dataset.page;
const titles = {servers: "Servers", capabilities: "Capabilities", policy: "Policy and limits", monitor: "Monitoring"};
const token = document.querySelector('meta[name="wyrit-token"]').content;
const editor = document.getElementById("editor");
const theme = document.getElementById("theme");
let state = {servers: [], limits: {}, overrides: {}};
let previous = null;
let edited = {};
let editorRevision;
let busy = false;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function button(label, action, className) {
  const element = node("button", label, className);
  element.type = "button";
  element.addEventListener("click", action);
  return element;
}

function table(headers, rows, empty) {
  if (!rows.length) return node("p", empty, "prose muted");
  const table = node("table");
  const head = table.createTHead().insertRow();
  for (const label of headers) {
    const cell = node("th", label);
    cell.scope = "col";
    head.append(cell);
  }
  const body = table.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    for (const item of row) {
      const cell = tr.insertCell();
      if (item instanceof Node) cell.append(item);
      else cell.textContent = item;
    }
  }
  return table;
}

function message(text, target = "message") {
  const element = document.getElementById(target);
  element.textContent = text;
  element.hidden = !text;
}

async function request(path, payload) {
  const options = payload ? {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Wyrit-Token": token},
    body: JSON.stringify(payload),
  } : {};
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error);
  return result;
}

async function refresh() {
  if (page === "monitor") {
    await refreshMonitoring(true);
    await renderCaptureSettings();
    return;
  }
  state = await request("/api/state");
  renderHealth();
  if (page === "servers") renderServers();
  if (page === "capabilities") renderCatalog();
  if (page === "policy") renderPolicy();
}

async function change(payload, target = "message") {
  if (busy) return false;
  busy = true;
  message(payload.action === "test" ? "Testing connection…" : "Applying configuration…", target);
  try {
    const result = await request("/api/config", {revision: state.revision, ...payload});
    if (payload.action !== "test") await refresh();
    message(result.message, target);
    return true;
  } catch (error) {
    message(error.message, target);
    return false;
  } finally {
    busy = false;
  }
}

const healthStates = {
  connected: ["●", "Connected", "ok"],
  connecting: ["◐", "Connecting", "warn"],
  pending: ["◐", "Pending reload", "warn"],
  down: ["×", "Down", "error"],
  disabled: ["–", "Disabled", "denied"],
};

function health(status, count) {
  const [glyph, label, color] = healthStates[status] || healthStates.pending;
  const element = node("span");
  const mark = node("span", glyph, "glyph " + color);
  mark.setAttribute("aria-hidden", "true");
  element.append(mark, node("span", count === undefined ? label : label + " " + count,
    status === "connected" ? "live" : ""));
  return element;
}

function renderHealth() {
  const counts = {};
  for (const server of state.servers) counts[server.health] = (counts[server.health] || 0) + 1;
  document.getElementById("health").replaceChildren(
    ...Object.keys(healthStates).map(status => health(status, counts[status] || 0))
  );
}

function renderServers() {
  const rows = state.servers.map(server => {
    const connection = node("div");
    connection.append(health(server.health));
    if (server.reason) connection.append(node("div", server.reason + ". Check the endpoint or executable.", "prose"));
    for (const [name, present] of Object.entries(server.headers)) {
      const reference = server.settings.headers[name];
      connection.append(node("div", name + " · " + reference.env + " · " + (present ? "Set" : "Missing")));
    }
    const tools = node("div", server.tools.map(tool => tool.name).join(", ") || "—");
    const actions = node("div", undefined, "actions");
    const enabled = server.settings.enabled !== false;
    actions.append(
      button("Edit", () => openEditor(server)),
      button(enabled ? "Disable" : "Enable", () => change({action: "enabled", name: server.name, enabled: !enabled})),
      button("Remove", () => {
        if (confirm("Remove " + server.name + "?")) change({action: "remove", name: server.name});
      }),
    );
    return [server.name, connection, tools, actions];
  });
  document.getElementById("server-list").replaceChildren(
    table(["Server", "Health", "Tools", "Actions"], rows, "No downstream servers configured.")
  );
}

function addHeader(name = "", reference = {}, presence = "") {
  const row = node("div", undefined, "fields");
  function field(label, value, required) {
    const wrapper = node("label", label);
    const input = node("input");
    input.value = value;
    input.required = required;
    input.spellcheck = false;
    wrapper.append(input);
    row.append(wrapper);
    return input;
  }
  const header = field("Header name", name, true);
  const env = field("Environment variable", reference.env ?? "", true);
  field("Prefix (optional)", reference.prefix ?? "", false);
  const status = node("span", presence);
  status.setAttribute("role", "status");
  env.addEventListener("input", () => { status.textContent = ""; });
  env.addEventListener("change", async () => {
    const value = env.value;
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(value.trim())) return;
    try {
      const result = await request("/api/config", {action: "header_status", env: value.trim()});
      if (env.value === value) status.textContent = result.present ? "Set" : "Missing";
    } catch (error) {
      if (env.value === value) status.textContent = error.message;
    }
  });
  const actions = node("div", undefined, "actions");
  actions.append(button("Remove header", () => row.remove()), status);
  row.append(actions);
  document.getElementById("header-rows").append(row);
  if (!name) header.focus();
}

function syncTransport() {
  const http = editor.elements.transport.value === "http";
  for (const field of editor.querySelectorAll(".http")) field.hidden = !http;
  document.getElementById("header-rows").closest("fieldset").disabled = !http;
  for (const field of editor.querySelectorAll(".stdio")) field.hidden = http;
  editor.elements.url.required = http;
  editor.elements.command.required = !http;
  editor.elements.url.disabled = !http;
  editor.elements.command.disabled = http;
}

function openEditor(server) {
  previous = server?.name ?? null;
  edited = server?.settings ?? {};
  editorRevision = state.revision;
  const fields = editor.elements;
  fields.name.value = previous ?? "";
  fields.namespace.value = edited.namespace ?? "";
  fields.transport.value = edited.transport ?? "http";
  fields.url.value = edited.url ?? "";
  fields.command.value = edited.command ?? "";
  fields.args.value = JSON.stringify(edited.args ?? []);
  document.getElementById("header-rows").replaceChildren();
  for (const [name, reference] of Object.entries(edited.headers ?? {})) {
    addHeader(name, reference, server.headers[name] ? "Set" : "Missing");
  }
  fields.expose.value = JSON.stringify(edited.expose ?? "all");
  fields.enabled.checked = edited.enabled !== false;
  syncTransport();
  document.getElementById("editor-title").textContent = previous ? "Edit " + previous : "Add downstream server";
  message("", "editor-message");
  editor.hidden = false;
  fields.name.focus();
}

function serverRequest(action) {
  const fields = editor.elements;
  const settings = {
    ...edited,
    namespace: fields.namespace.value.trim(),
    transport: fields.transport.value,
    expose: JSON.parse(fields.expose.value),
    enabled: fields.enabled.checked,
  };
  for (const key of ["url", "headers", "command", "args"]) delete settings[key];
  if (settings.transport === "http") {
    settings.url = fields.url.value.trim();
    const headers = [];
    const names = new Set();
    for (const row of document.querySelectorAll("#header-rows > div")) {
      const [name, variable, prefix] = row.querySelectorAll("input");
      const key = name.value.trim();
      if (names.has(key.toLowerCase())) throw new Error("Use each header name once.");
      names.add(key.toLowerCase());
      const reference = {env: variable.value.trim()};
      if (prefix.value) reference.prefix = prefix.value;
      headers.push([key, reference]);
    }
    settings.headers = Object.fromEntries(headers);
  } else {
    settings.command = fields.command.value;
    settings.args = JSON.parse(fields.args.value || "[]");
  }
  return {action, name: fields.name.value.trim(), previous, settings, revision: editorRevision};
}

async function submitServer(action) {
  if (!editor.reportValidity() || busy) return;
  let payload;
  try {
    payload = serverRequest(action);
  } catch (error) {
    message(error instanceof SyntaxError ? "Check the JSON in Exposure or Arguments." : error.message, "editor-message");
    return;
  }
  if (await change(payload, "editor-message") && action === "save") {
    editor.hidden = true;
    message("Configuration saved.");
    document.getElementById("add").focus();
  }
}

function renderCatalog() {
  const query = document.getElementById("query").value.trim().toLowerCase();
  const rows = [];
  for (const server of state.servers) {
    for (const tool of server.tools) {
      if (![server.name, tool.name, tool.id, tool.description, tool.classification]
        .join(" ").toLowerCase().includes(query)) continue;
      const descriptor = node("div", tool.id || server.name + " / " + tool.name);
      if (tool.id) {
        const details = node("details");
        details.append(node("summary", "Schema"),
          node("p", tool.description, "prose"),
          node("pre", JSON.stringify({input: tool.input_schema, output: tool.output_schema}, null, 2)));
        descriptor.append(details);
      }
      const toggle = button(tool.exposed ? "Hide" : "Expose", () => change({
        action: "exposure", name: server.name, tool: tool.name, exposed: !tool.exposed,
      }));
      toggle.setAttribute("aria-label", toggle.textContent + " " + tool.name);
      rows.push([descriptor, tool.classification || "Unknown", toggle]);
    }
  }
  document.getElementById("catalog").replaceChildren(
    table(["Capability", "Class", "Exposure"], rows, "No capabilities match.")
  );
}

function policySelect(label, options, value, payload) {
  const select = node("select");
  select.setAttribute("aria-label", label);
  for (const [key, text] of options) select.add(new Option(text, key));
  select.value = value;
  select.addEventListener("change", async () => {
    const denied = select.value === "inherit" ? null : select.value === "deny";
    if (!await change({...payload, action: "policy", deny_writes: denied})) select.value = value;
  });
  return select;
}

function renderPolicy() {
  const options = [["deny", "Deny"], ["allow", "Allow"]];
  const rows = [];
  for (const server of state.servers) {
    const denied = server.settings.deny_writes !== false;
    rows.push([server.name, "All writes", policySelect(server.name + " writes", options,
      denied ? "deny" : "allow", {name: server.name})]);
    for (const tool of server.tools) {
      const selected = server.settings.expose === "all" || server.settings.expose.includes(tool.name);
      if (!selected) continue;
      const override = server.settings.capabilities?.[tool.name]?.deny_writes;
      rows.push([server.name, tool.name, policySelect(server.name + " " + tool.name + " writes",
        [["inherit", "Inherit · " + (denied ? "deny" : "allow")], ...options],
        override === undefined ? "inherit" : override ? "deny" : "allow",
        {name: server.name, tool: tool.name})]);
    }
  }
  document.getElementById("policies").replaceChildren(
    table(["Server", "Scope", "Writes"], rows, "Add a server to configure write allowances.")
  );
  const limits = document.getElementById("limits");
  limits.replaceChildren(limitBlock("Global", null, state.limits.global, state.overrides));
  for (const server of state.servers) {
    limits.append(limitBlock(server.name, server.name, state.limits.servers[server.settings.namespace],
      server.settings.limits ?? {}));
  }
}

function limitBlock(title, name, effective, overrides) {
  const form = node("form", undefined, "limit-block");
  form.append(node("h3", title));
  const fields = {};
  const rows = Object.entries(effective).map(([key, limit]) => {
    const input = node("input");
    input.type = "number";
    input.min = "0";
    input.step = key.endsWith("timeout") || key.endsWith("deadline") ? "any" : "1";
    input.value = overrides[key] ?? "";
    input.placeholder = "Inherit";
    input.setAttribute("aria-label", title + " " + key + " override");
    fields[key] = input;
    return [key, String(limit.value), limit.source, input];
  });
  const actions = node("div", undefined, "actions");
  actions.append(node("button", "Save limits", "primary"));
  form.append(table(["Limit", "Effective", "Source", "Override"], rows, ""), actions);
  form.addEventListener("submit", event => {
    event.preventDefault();
    const values = Object.entries(fields).filter(([, field]) => field.value !== "");
    change({action: "limits", name, limits: Object.fromEntries(values.map(([key, field]) => [key, field.valueAsNumber]))});
  });
  return form;
}

let feedCursor = 0;
let monitorEpoch = "";
let sourceRevision = "";
let feedStart = 0;
const feedSourceUpdates = new Map();
let monitorBusy = false;
let monitorRepeat = false;
let feedPaused = false;
const monitorWindow = document.getElementById("monitor-window");
const marks = {SUCCEEDED: ["✓", "ok"], FAILED: ["×", "error"], DENIED: ["⊘", "denied"], UNKNOWN: ["?", "warn"]};

function outcome(value) {
  const label = node("span");
  const runMarks = {SUCCESS: marks.SUCCEEDED, TIMED_OUT: ["◷", "warn"], CANCELLED: ["⊘", "muted"]};
  const [glyph, style] = marks[value] ?? runMarks[value] ?? ["·", "muted"];
  const mark = node("span", glyph, "glyph " + style);
  mark.setAttribute("aria-hidden", "true");
  label.append(mark, node("span", value));
  return label;
}

function monitorUrl(path, query) {
  return "/api/" + path + "?" + new URLSearchParams({epoch: monitorEpoch, ...query});
}

function sourceLink(ref) {
  const link = node("a", ref);
  link.href = monitorUrl("source", {ref});
  link.target = "_blank";
  link.rel = "noopener";
  return link;
}

function sources(refs, label = "Ledger sources") {
  if (!refs.length) return "—";
  const details = node("details");
  details.append(node("summary", label));
  details.addEventListener("toggle", () => {
    if (!details.open || details.childElementCount > 1) return;
    const list = node("div", undefined, "actions");
    for (const ref of refs) list.append(sourceLink(ref));
    details.append(list);
  });
  return details;
}

function elapsed(seconds) {
  return seconds == null ? "—" : (seconds * 1000).toFixed(2) + " ms";
}

function timeLabel(seconds) {
  return new Date(seconds * 1000).toLocaleString();
}

function capabilityName(name) {
  if (name == null) return "—";
  const label = node("span", undefined, "capability-name");
  name.split(/(?<=[._])/u).forEach((part, index) => {
    if (index) label.append(node("wbr"));
    label.append(document.createTextNode(part));
  });
  return label;
}

function runCard(row, preview) {
  const run = row.record;
  const article = node("details", undefined, "run-row");
  article.dataset.ended = run.ended_at;
  const summary = node("summary");
  const fields = node("span", undefined, "run-fields");
  const sequence = run.actions.map(action => action.capability?.split(".").at(-1) ?? "—");
  const shortNames = sequence.join(" → ");
  const calls = node("span", shortNames || "No calls", "run-sequence");
  calls.title = shortNames;
  const firstLine = node("code", undefined, "source-preview");
  fields.append(node("time", timeLabel(run.ended_at)), outcome(run.outcome), calls,
    node("span", elapsed(run.ended_at - run.started_at)), node("span", run.run_id.slice(0, 8)), firstLine);
  summary.append(fields);
  const source = node("pre", undefined, "run-source");
  const unavailable = run.source_status === "withheld" ?
    "source withheld: looked like a credential" : "No retained source";
  let previewExpiry;
  let sourceExpiry;
  function showPreview(value) {
    clearTimeout(previewExpiry);
    firstLine.textContent = value ? value.text : unavailable;
    if (value) previewExpiry = setTimeout(() => showPreview(null), value.remaining_seconds * 1000);
  }
  showPreview(preview);

  const deniedIndex = run.actions.findIndex(action => action.outcome === "DENIED");
  const denied = run.actions[deniedIndex];
  const deadline = (run.limit_hits ?? [run.limit_hit]).some(hit => hit?.name === "run_deadline");
  const reason = run.reason ?? (run.outcome === "TIMED_OUT" && deadline ? "Run deadline exceeded" :
    denied ? "Call " + (denied.call_id ?? deniedIndex + 1) + " denied" + (denied.reason === "WRITE_POLICY" ? " by write policy" : "") : null);
  const id = node("p", undefined, "run-id");
  id.append(document.createTextNode("Run " + run.run_id + " · Ledger "), sourceLink(row.ref));
  article.append(summary, id);
  if (reason) article.append(node("p", reason));
  article.append(source);
  article.append(table(["Call", "Capability", "Outcome", "Duration", "Result size"],
    run.actions.map((action, index) => {
      const status = outcome(action.outcome);
      if (action.outcome === "UNKNOWN") status.append(node("div", "Check the downstream system.", "prose"));
      return [action.call_id ?? index + 1, capabilityName(action.capability), status,
        elapsed(action.duration_seconds), action.result_size_bytes == null ? "—" : action.result_size_bytes + " B"];
    }), "No recorded calls."));
  const capture = node("details");
  const payload = node("pre");
  const url = monitorUrl("captures", {ref: row.ref});
  capture.append(node("summary", "Captured payloads"), payload);
  let requestVersion = 0;
  async function loadCapture() {
    const version = ++requestVersion;
    clearTimeout(sourceExpiry);
    payload.textContent = "";
    source.textContent = "";
    if (!article.open) return;
    source.textContent = "Loading source…";
    try {
      const data = await request(url);
      if (version !== requestVersion || !article.open) return;
      source.textContent = data.source?.text ?? unavailable;
      if (data.source) {
        sourceExpiry = setTimeout(() => { source.textContent = unavailable; },
          data.source.remaining_seconds * 1000);
      }
      payload.textContent = data.payloads.length ? JSON.stringify(data.payloads, null, 2) :
        "No retained payloads for this run.";
    } catch (error) {
      if (version === requestVersion && article.open) source.textContent = error.message;
    }
  }
  article.addEventListener("toggle", loadCapture);
  feedSourceUpdates.set(row.ref, value => {
    showPreview(value);
    if (article.open) loadCapture();
  });
  article.append(capture);
  return article;
}

function renderMonitoring(data, requestedPaused, force) {
  const reset = data.epoch !== monitorEpoch;
  if (reset) {
    document.getElementById("run-feed").replaceChildren();
    feedStart = Math.max(0, data.cursor - 50);
    feedCursor = feedStart;
    feedSourceUpdates.clear();
  }
  if (sourceRevision !== data.source_revision) {
    for (const [ref, update] of feedSourceUpdates) update(data.previews[ref]);
    sourceRevision = data.source_revision;
  }
  monitorEpoch = data.epoch;
  if (!feedPaused && !requestedPaused) {
    const feed = document.getElementById("run-feed");
    feed.querySelector(".empty-feed")?.remove();
    let existing = feed.firstElementChild;
    for (const row of data.feed) {
      while (existing && Number(existing.dataset.ended) > row.record.ended_at) {
        existing = existing.nextElementSibling;
      }
      feed.insertBefore(runCard(row, data.previews[row.ref]), existing);
    }
    if (!feed.childElementCount) feed.append(node("p", "No recorded runs.", "prose muted empty-feed"));
    feedCursor = data.cursor;
  }
  if (!reset && !force && (document.activeElement.closest("#monitor table") ||
      document.querySelector("#health details[open], #capability-stats details[open], #server-history details[open]"))) return;
  const strip = data.health;
  const status = document.getElementById("health");
  const attention = node("div", undefined, "attention");
  attention.append(node("strong", "Needs attention"));
  if (!strip.attention.length && !data.health_record_error) attention.append(node("span", "None"));
  for (const item of strip.attention) {
    attention.append(sources(item.refs, item.label));
  }
  if (data.health_record_error) attention.append(node("span", "Some health records could not be written.", "warn"));
  status.replaceChildren(
    attention,
    strip.refs.length ? sources(strip.refs, strip.connected + " connected · " +
      strip.exposed + " exposed capabilities") : node("span", "0 connected · 0 exposed capabilities"),
    strip.run_refs.length ? sources(strip.run_refs, strip.runs + " runs in the last hour") :
      node("span", "0 runs in the last hour"),
  );
  const capture = data.capture_enabled;
  document.getElementById("capture-summary").textContent =
    "Generated code is on by default for 7 days. Arguments and results are off by default. " +
    (capture.source_overrides ? capture.source_overrides + " source retention overrides. " : "") +
    (capture.payload_servers ? "Arguments and results on for " + capture.payload_servers + " servers." : "");
  const rate = value => (value * 100).toFixed(1) + "%";
  document.getElementById("capability-stats").replaceChildren(table(
    ["Capability", "Calls", ...Object.keys(marks), "p50", "p95", "Sources"],
    data.capabilities.map(stats => [capabilityName(stats.capability), stats.calls, ...Object.keys(marks)
      .map(key => rate(stats.rates[key])), elapsed(stats.p50), elapsed(stats.p95), sources(stats.refs)]),
    "No named calls in this window."
  ));
  document.getElementById("server-history").replaceChildren(table(
    ["Server", "Connection history", "Reconnects", "Calls lost to drops"],
    Object.entries(data.servers).map(([name, server]) => {
      const history = node("details");
      history.append(node("summary", "Recorded health transitions"));
      history.addEventListener("toggle", () => {
        if (!history.open || history.childElementCount > 1) return;
        history.append(table(["Time", "Status", "Host", "Reason", "Source"],
          [...server.history].reverse().map(row => [timeLabel(row.record.at),
            row.record.status, row.record.host, row.record.reason ?? "—", sourceLink(row.ref)]), ""));
      });
      const reconnects = node("div", String(server.reconnects.length));
      if (server.reconnects.length) reconnects.append(sources(server.reconnects));
      const drops = node("div", String(server.drops.length));
      if (server.drops.length) drops.append(sources([...new Set(server.drops.map(drop => drop.ref))]));
      return [name, history, reconnects, drops];
    }), "No recorded server history."
  ));
  const limits = data.limits.map(limit => [
    "Limit hit", limit.name, limit.namespace ?? "Global", limit.value ?? "—", sourceLink(limit.ref),
  ]);
  const denials = data.denials.map(denial => [
    "Write denied", capabilityName(denial.capability), "Call " + denial.call, "—", sourceLink(denial.ref),
  ]);
  document.getElementById("policy-history").replaceChildren(table(
    ["Event", "Limit or capability", "Scope", "Limit value", "Source"],
    [...limits, ...denials], "No recorded limit hits or write denials in this window."
  ));
}

async function refreshMonitoring(force = false) {
  if (monitorBusy) {
    monitorRepeat ||= force;
    return;
  }
  if (!force && document.hidden) return;
  monitorBusy = true;
  const requestedPaused = feedPaused;
  try {
    const data = await request(monitorUrl("monitor", {
      window: monitorWindow.value, after: feedCursor, paused: requestedPaused ? "1" : "0",
      start: feedStart, source_revision: sourceRevision,
    }));
    renderMonitoring(data, requestedPaused, force);
    message("", "monitor-message");
  } catch (error) {
    message(error.message, "monitor-message");
  } finally {
    monitorBusy = false;
    if (monitorRepeat) {
      monitorRepeat = false;
      refreshMonitoring(true);
    }
  }
}

async function renderCaptureSettings() {
  const data = await request("/api/config", {action: "capture_status"});
  document.getElementById("capture-settings").replaceChildren(table(
    ["Server", "Generated code", "Arguments and results"],
    data.servers.map(server => {
      function controls(action, seconds, defaultMinutes, label) {
        const minutes = node("input");
        minutes.type = "number";
        minutes.min = "1";
        minutes.max = "10080";
        minutes.step = "any";
        minutes.required = true;
        minutes.value = seconds ? seconds / 60 : defaultMinutes;
        minutes.setAttribute("aria-label", server.name + " " + label + " retention in minutes");
        const actions = node("div", undefined, "actions");
        async function save(nextSeconds) {
          if (busy) return;
          busy = true;
          try {
            await request("/api/config", {
              action, name: server.name, namespace: server.namespace, seconds: nextSeconds,
            });
            await refreshMonitoring(true);
            await renderCaptureSettings();
            message("Capture settings saved.");
          } catch (error) {
            message(error.message);
          } finally {
            busy = false;
          }
        }
        const retention = node("label", seconds ? "On · retention in minutes" : "Off · retention in minutes");
        retention.append(minutes);
        actions.append(retention, button(seconds ? "Update retention" : "Turn on", () => {
          if (minutes.reportValidity()) save(Math.round(60 * minutes.valueAsNumber));
        }));
        if (seconds) actions.append(button("Turn off and purge", () => save(0)));
        return actions;
      }
      return [server.name, controls("capture_source", server.source_seconds, 10080, "source"),
        controls("capture", server.seconds, 60, "payload")];
    }), "Add a server to configure capture."
  ));
}

document.getElementById("title").textContent = titles[page];
document.title = titles[page] + " · Wyrit";
document.getElementById(page).hidden = false;
document.querySelector('[data-target="' + page + '"]').setAttribute("aria-current", "page");
theme.value = localStorage.getItem("wyrit-theme") || "system";
document.documentElement.dataset.theme = theme.value;
theme.addEventListener("change", () => {
  localStorage.setItem("wyrit-theme", theme.value);
  document.documentElement.dataset.theme = theme.value;
});
document.getElementById("reload").addEventListener("click", () => change({action: "reload"}));
document.getElementById("add").addEventListener("click", () => openEditor());
document.getElementById("cancel").addEventListener("click", () => { editor.hidden = true; });
document.getElementById("test").addEventListener("click", () => submitServer("test"));
editor.elements.transport.addEventListener("change", syncTransport);
document.getElementById("add-header").addEventListener("click", () => addHeader());
editor.addEventListener("submit", event => {
  event.preventDefault();
  submitServer("save");
});
document.getElementById("query").addEventListener("input", renderCatalog);
monitorWindow.addEventListener("change", () => refreshMonitoring(true));
document.getElementById("pause-feed").addEventListener("click", event => {
  feedPaused = !feedPaused;
  event.currentTarget.setAttribute("aria-pressed", String(feedPaused));
  event.currentTarget.textContent = feedPaused ? "Resume feed" : "Pause feed";
  if (!feedPaused) refreshMonitoring(true);
});
refresh().catch(error => message(error.message));
setInterval(async () => {
  if (page === "monitor") return refreshMonitoring();
  if (busy || !editor.hidden || document.hidden || document.activeElement.closest("table")) return;
  try {
    const next = await request("/api/state");
    if (busy || !editor.hidden || next.revision !== state.revision) return;
    if (JSON.stringify(next.servers) === JSON.stringify(state.servers)) return;
    state = next;
    renderHealth();
    if (page === "servers") renderServers();
    if (page === "capabilities") renderCatalog();
  } catch {
    // Keep the current view available when a background health check fails.
  }
}, 5000);
