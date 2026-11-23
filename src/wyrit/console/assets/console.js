"use strict";

const page = document.body.dataset.page;
const titles = {servers: "Servers", capabilities: "Capabilities", policy: "Policy and limits"};
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

function syncTransport() {
  const http = editor.elements.transport.value === "http";
  for (const field of editor.querySelectorAll(".http")) field.hidden = !http;
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
  fields.headers.value = JSON.stringify(edited.headers ?? {}, null, 2);
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
    settings.headers = JSON.parse(fields.headers.value || "{}");
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
  } catch {
    message('Check the JSON in Exposure and in Arguments or Header references.', "editor-message");
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
editor.addEventListener("submit", event => {
  event.preventDefault();
  submitServer("save");
});
document.getElementById("query").addEventListener("input", renderCatalog);
refresh().catch(error => message(error.message));
setInterval(async () => {
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
