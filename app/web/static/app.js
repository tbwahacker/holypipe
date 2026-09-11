// HolyPipe dashboard — vanilla JS, no build step.
const API = "/api";

const state = {
  sources: [],
  destinations: [],
  connections: [],
  connectorTypes: { sources: [], destinations: [] },
  discoverCache: {}, // sourceId -> streams
};

const TYPE_ICON = { postgres: "🐘", mysql: "🐬", sqlite: "🗄️" };
function typeIcon(type) { return TYPE_ICON[type] || "🔌"; }

// ---------------------------------------------------------------------------
// tiny helpers
// ---------------------------------------------------------------------------
function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c === null || c === undefined) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let body = null;
  try { body = await res.json(); } catch { /* no body */ }
  if (!res.ok) {
    const msg = (body && (body.detail || body.message)) || res.statusText;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return body;
}

function toast(message, kind = "ok") {
  const stack = document.getElementById("toastStack");
  const node = el("div", { class: `toast ${kind}` }, message);
  stack.appendChild(node);
  setTimeout(() => node.remove(), 4500);
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString();
}

function closeModal() {
  document.getElementById("modalBackdrop").classList.remove("open");
  const modal = document.getElementById("modal");
  modal.innerHTML = "";
  modal.classList.remove("wide");
}

function openModal(contentNode, opts = {}) {
  const modal = document.getElementById("modal");
  modal.innerHTML = "";
  modal.classList.toggle("wide", !!opts.wide);
  modal.appendChild(contentNode);
  document.getElementById("modalBackdrop").classList.add("open");
}

document.getElementById("modalBackdrop").addEventListener("click", (e) => {
  if (e.target.id === "modalBackdrop") closeModal();
});

// ---------------------------------------------------------------------------
// tabs
// ---------------------------------------------------------------------------
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("panel-" + btn.dataset.tab).classList.add("active");
  });
});

// ---------------------------------------------------------------------------
// connector type forms
// ---------------------------------------------------------------------------
function fieldNode(field, value) {
  const wrap = el("div", { class: field.kind === "checkbox" ? "field checkbox" : "field" });
  const id = "f_" + field.name;
  if (field.kind === "checkbox") {
    const input = el("input", { type: "checkbox", id, name: field.name });
    input.checked = value !== undefined ? !!value : !!field.default;
    wrap.append(input, el("label", { for: id }, field.label));
    return wrap;
  }
  const label = el("label", { for: id }, field.label + (field.required ? " *" : ""));
  if (field.kind === "select") {
    const select = el("select", { id, name: field.name },
      (field.options || []).map((opt) => el("option", { value: opt }, opt)));
    if (value !== undefined && value !== null) select.value = value;
    else if (field.default !== undefined) select.value = field.default;
    wrap.append(label, select);
    return wrap;
  }
  const type = field.kind === "password" ? "password" : field.kind === "number" ? "number" : "text";
  const input = el("input", { type, id, name: field.name,
    placeholder: field.default !== undefined ? String(field.default) : "" });
  if (value !== undefined && value !== null) input.value = value;
  else if (field.default !== undefined && field.kind === "number") input.value = field.default;
  wrap.append(label, input);
  return wrap;
}

function readConfigForm(form, fields) {
  const config = {};
  for (const f of fields) {
    const input = form.querySelector(`[name="${f.name}"]`);
    if (!input) continue;
    if (f.kind === "checkbox") config[f.name] = input.checked;
    else if (f.kind === "number") config[f.name] = input.value === "" ? null : Number(input.value);
    else config[f.name] = input.value;
  }
  return config;
}

// ---------------------------------------------------------------------------
// sources / destinations: list + create/edit modal
// ---------------------------------------------------------------------------
function connectorCard(item, kind) {
  const usedBy = state.connections.filter((c) =>
    kind === "source" ? c.source_id === item.id : c.destination_id === item.id);
  const card = el("div", { class: "card" });
  card.append(
    el("div", { class: "card-top" }, [
      el("div", {}, [
        el("div", { class: "card-title" }, [
          el("span", { class: "icon" }, typeIcon(item.type)),
          item.name,
          el("span", { class: `badge ${item.type}` }, item.type),
        ]),
        el("div", { class: "card-sub" }, `${usedBy.length} connection(s) using this`),
      ]),
      el("div", { class: "card-actions" }, [
        el("button", { class: "btn small", onclick: () => testConnector(item, kind) }, "Test"),
        el("button", { class: "btn small", onclick: () => openConnectorModal(kind, item) }, "Edit"),
        el("button", { class: "btn small danger", onclick: () => deleteConnector(item, kind) }, "Delete"),
      ]),
    ])
  );
  return card;
}

async function testConnector(item, kind) {
  toast(`Testing ${item.name}…`, "ok");
  try {
    const res = await api(`/${kind}s/${item.id}/test`, { method: "POST" });
    toast(res.ok ? `✓ ${res.message}` : `✗ ${res.message}`, res.ok ? "ok" : "err");
  } catch (e) {
    toast(e.message, "err");
  }
}

async function deleteConnector(item, kind) {
  if (!confirm(`Delete ${kind} "${item.name}"?`)) return;
  try {
    await api(`/${kind}s/${item.id}`, { method: "DELETE" });
    toast("Deleted", "ok");
    await refreshAll();
  } catch (e) {
    toast(e.message, "err");
  }
}

const URI_PLACEHOLDERS = {
  postgres: "postgresql://user:password@host:5432/dbname?sslmode=require",
  mysql: "mysql://user:password@host:3306/dbname?ssl_mode=REQUIRED",
  sqlite: "sqlite:////data/app.db",
};

function openConnectorModal(kind, existing) {
  const types = kind === "source" ? state.connectorTypes.sources : state.connectorTypes.destinations;
  const isEdit = !!existing;
  const form = el("form", {});
  const nameField = el("div", { class: "field" }, [
    el("label", {}, "Name *"),
    el("input", { type: "text", name: "_name", required: "true", value: existing ? existing.name : "" }),
  ]);
  const typeSelect = el("select", { name: "_type" },
    types.map((t) => el("option", { value: t.type }, `${typeIcon(t.type)} ${t.type}`)));
  if (existing) typeSelect.value = existing.type;
  const typeField = el("div", { class: "field" }, [el("label", {}, "Type"), typeSelect]);
  const fieldsWrap = el("div", { class: "form-grid" });

  let inputMode = "form"; // "form" | "uri"
  const formModeBtn = el("button", { type: "button", class: "btn small primary" }, "Form fields");
  const uriModeBtn = el("button", { type: "button", class: "btn small" }, "Connection URI");
  const modeToggle = el("div", { class: "seg-toggle" }, [formModeBtn, uriModeBtn]);

  const uriInput = el("input", { type: "text", name: "_uri", placeholder: URI_PLACEHOLDERS[typeSelect.value] || "" });
  const uriWrap = el("div", { class: "field span2", style: "display:none" }, [
    el("label", {}, "Connection URI"),
    uriInput,
    el("div", { class: "hint" }, "Paste a full connection string instead of filling in fields below."),
  ]);

  function setMode(mode) {
    inputMode = mode;
    formModeBtn.classList.toggle("primary", mode === "form");
    uriModeBtn.classList.toggle("primary", mode === "uri");
    fieldsWrap.style.display = mode === "form" ? "grid" : "none";
    uriWrap.style.display = mode === "uri" ? "block" : "none";
  }
  formModeBtn.addEventListener("click", () => setMode("form"));
  uriModeBtn.addEventListener("click", () => setMode("uri"));

  function renderFields(typeName) {
    fieldsWrap.innerHTML = "";
    const spec = types.find((t) => t.type === typeName);
    const values = existing && existing.type === typeName ? existing.config : {};
    (spec ? spec.fields : []).forEach((f) => fieldsWrap.appendChild(fieldNode(f, values[f.name])));
    uriInput.placeholder = URI_PLACEHOLDERS[typeName] || "";
  }
  renderFields(typeSelect.value);
  typeSelect.addEventListener("change", () => renderFields(typeSelect.value));
  setMode("form");

  function buildPayload(probeName) {
    const type = typeSelect.value;
    if (inputMode === "uri") {
      const uri = uriInput.value.trim();
      if (!uri) throw new Error("Connection URI is required");
      return { name: probeName, type, uri };
    }
    const spec = types.find((t) => t.type === type);
    return { name: probeName, type, config: readConfigForm(form, spec.fields) };
  }

  const testResult = el("div", { class: "field hint" }, "");
  const actions = el("div", { class: "modal-actions" }, [
    el("button", { type: "button", class: "btn", onclick: async () => {
      testResult.textContent = "Testing…";
      try {
        const payload = buildPayload("_probe");
        const res = await api(`/${kind}s/test-config`, { method: "POST", body: JSON.stringify(payload) });
        testResult.textContent = res.ok ? `✓ ${res.message}` : `✗ ${res.message}`;
        testResult.style.color = res.ok ? "var(--ok)" : "var(--err)";
      } catch (e) {
        testResult.textContent = "✗ " + e.message;
        testResult.style.color = "var(--err)";
      }
    } }, "Test connection"),
    el("button", { type: "button", class: "btn", onclick: closeModal }, "Cancel"),
    el("button", { type: "submit", class: "btn primary" }, isEdit ? "Save changes" : "Save"),
  ]);

  form.append(
    el("h3", {}, isEdit ? `Edit ${kind} — ${existing.name}` : `New ${kind}`),
    nameField, typeField, modeToggle, fieldsWrap, uriWrap, testResult, actions
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = form.querySelector('[name="_name"]').value.trim();
    if (!name) return toast("Name is required", "err");
    try {
      const payload = buildPayload(name);
      if (isEdit) {
        await api(`/${kind}s/${existing.id}`, { method: "PUT", body: JSON.stringify(payload) });
        toast(`${kind} updated`, "ok");
      } else {
        await api(`/${kind}s`, { method: "POST", body: JSON.stringify(payload) });
        toast(`${kind} created`, "ok");
      }
      closeModal();
      await refreshAll();
    } catch (e2) {
      toast(e2.message, "err");
    }
  });
  openModal(form);
}

document.getElementById("newSourceBtn").addEventListener("click", () => openConnectorModal("source"));
document.getElementById("newDestinationBtn").addEventListener("click", () => openConnectorModal("destination"));

// ---------------------------------------------------------------------------
// connections
// ---------------------------------------------------------------------------
function statusBadge(status) {
  return el("span", { class: `status ${status}` }, status);
}

function connectionCard(conn) {
  const source = state.sources.find((s) => s.id === conn.source_id);
  const destination = state.destinations.find((d) => d.id === conn.destination_id);
  const card = el("div", { class: "card clickable", id: "conn-" + conn.id });
  card.addEventListener("click", (e) => {
    if (e.target.closest(".card-actions")) return;
    openConnectionDetail(conn);
  });

  const actions = [];
  if (conn.mode === "batch") {
    actions.push(el("button", {
      class: "btn small primary",
      disabled: (conn.status === "running" || !conn.enabled) ? "true" : null,
      onclick: () => runConnection(conn),
    }, conn.status === "running" ? "Running…" : "Run now"));
  } else {
    const isStreaming = conn.status === "streaming";
    actions.push(el("button", {
      class: "btn small primary", onclick: () => toggleCdc(conn, !isStreaming),
    }, isStreaming ? "Stop" : "Start"));
  }
  actions.push(el("button", {
    class: "btn small", onclick: () => toggleActive(conn),
  }, conn.enabled ? "Deactivate" : "Activate"));
  actions.push(el("button", { class: "btn small", onclick: () => openConnectionEditModal(conn) }, "Edit"));
  actions.push(el("button", { class: "btn small", onclick: () => viewRuns(conn) }, "Runs"));
  actions.push(el("button", { class: "btn small danger", onclick: () => deleteConnection(conn) }, "Delete"));

  let frequencyLabel;
  if (conn.mode === "cdc") {
    frequencyLabel = "real-time (CDC)";
  } else if (conn.schedule_type === "cron") {
    frequencyLabel = `cron: ${conn.cron_expression}`;
  } else {
    frequencyLabel = `every ${conn.interval_seconds}s`;
  }

  card.append(
    el("div", { class: "card-top" }, [
      el("div", {}, [
        el("div", { class: "card-title" }, [
          conn.name,
          statusBadge(conn.status),
          el("span", { class: `badge ${conn.enabled ? "" : "paused"}` }, conn.enabled ? "active" : "paused"),
          el("span", { class: "badge" }, frequencyLabel),
        ]),
        el("div", { class: "card-sub" },
          `${typeIcon(source && source.type)} ${source ? source.name : "?"} → ${typeIcon(destination && destination.type)} ${destination ? destination.name : "?"}`),
        conn.status_detail ? el("div", { class: "card-sub", style: "color:var(--err)" }, conn.status_detail) : null,
      ]),
      el("div", { class: "card-actions" }, actions),
    ]),
    el("div", { class: "card-body" }, [
      el("div", { class: "card-stat" }, [el("b", {}, String(conn.streams.filter(s => s.selected !== false).length)), "streams"]),
      el("div", { class: "card-stat" }, [el("b", {}, fmtTime(conn.last_run_at)), "last sync"]),
      el("div", { class: "card-stat" }, [
        el("b", {}, conn.mode === "cdc" ? (conn.status === "streaming" ? "streaming" : "—") : fmtTime(conn.next_run_at)),
        "next run",
      ]),
      el("div", { class: "card-stat", id: `stat-read-${conn.id}` }, [el("b", {}, "—"), "records read (live)"]),
      el("div", { class: "card-stat", id: `stat-written-${conn.id}` }, [el("b", {}, "—"), "records written (live)"]),
    ])
  );
  return card;
}

async function runConnection(conn) {
  try {
    await api(`/connections/${conn.id}/run`, { method: "POST" });
    toast(`Sync started for ${conn.name}`, "ok");
    await refreshConnections();
  } catch (e) {
    toast(e.message, "err");
  }
}

/** Forces a full reload — drops any stored incremental/xmin cursor first,
 * unlike a plain run which only picks up the delta. `streamNames` scopes it
 * to specific tables (batch only); omit for the whole connection. */
async function resyncConnection(conn, streamNames) {
  try {
    await api(`/connections/${conn.id}/resync`, {
      method: "POST",
      body: JSON.stringify({ stream_names: streamNames || null }),
    });
    toast(streamNames ? `Resyncing ${streamNames.length} table(s)` : `Resyncing ${conn.name}`, "ok");
    await refreshConnections();
  } catch (e) {
    toast(e.message, "err");
  }
}

async function toggleCdc(conn, start) {
  try {
    await api(`/connections/${conn.id}/${start ? "start" : "stop"}`, { method: "POST" });
    toast(`${conn.name} ${start ? "streaming started" : "stopped"}`, "ok");
    await refreshConnections();
  } catch (e) {
    toast(e.message, "err");
  }
}

async function toggleActive(conn) {
  try {
    if (conn.mode === "cdc") {
      // /start and /stop already flip `enabled` together with actually
      // starting/stopping the streaming worker.
      await api(`/connections/${conn.id}/${conn.enabled ? "stop" : "start"}`, { method: "POST" });
    } else {
      await api(`/connections/${conn.id}`, {
        method: "PATCH", body: JSON.stringify({ enabled: !conn.enabled }),
      });
    }
    toast(conn.enabled ? `${conn.name} deactivated` : `${conn.name} activated`, "ok");
    await refreshConnections();
  } catch (e) {
    toast(e.message, "err");
  }
}

async function deleteConnection(conn) {
  if (!confirm(`Delete connection "${conn.name}"?`)) return;
  try {
    await api(`/connections/${conn.id}`, { method: "DELETE" });
    toast("Deleted", "ok");
    await refreshAll();
  } catch (e) {
    toast(e.message, "err");
  }
}

async function viewRuns(conn) {
  const wrap = el("div", {});
  wrap.append(el("h3", {}, `Runs — ${conn.name}`));
  const list = el("div", { class: "stream-list" }, el("div", { class: "hint" }, "Loading…"));
  wrap.append(list);
  wrap.append(el("div", { class: "modal-actions" }, [
    el("button", { type: "button", class: "btn", onclick: closeModal }, "Close"),
  ]));
  openModal(wrap);
  try {
    const runs = await api(`/connections/${conn.id}/runs`);
    list.innerHTML = "";
    if (!runs.length) list.append(el("div", { class: "hint" }, "No runs yet."));
    for (const r of runs) {
      list.append(el("div", { class: "stream-row" }, [
        el("span", { class: "name" }, `${fmtTime(r.started_at)} · ${r.trigger}`),
        statusBadge(r.status),
        el("span", {}, `${r.records_read} read / ${r.records_written} written`),
      ]));
      if (r.error) list.append(el("div", { class: "card-sub", style: "color:var(--err)" }, r.error));
    }
  } catch (e) {
    list.innerHTML = "";
    list.append(el("div", { class: "hint" }, e.message));
  }
}

// ---------------------------------------------------------------------------
// connection detail view (opened by clicking a connection card)
// ---------------------------------------------------------------------------
function openConnectionDetail(conn) {
  const source = state.sources.find((s) => s.id === conn.source_id);
  const destination = state.destinations.find((d) => d.id === conn.destination_id);
  const wrap = el("div", {});

  let frequencyLabel;
  if (conn.mode === "cdc") {
    frequencyLabel = "real-time (CDC)";
  } else if (conn.schedule_type === "cron") {
    frequencyLabel = `cron: ${conn.cron_expression}`;
  } else {
    frequencyLabel = `every ${conn.interval_seconds}s`;
  }

  const actionsRow = el("div", { class: "card-actions" }, []);
  if (conn.mode === "batch") {
    actionsRow.append(el("button", {
      class: "btn small primary",
      disabled: (conn.status === "running" || !conn.enabled) ? "true" : null,
      onclick: async () => { await runConnection(conn); closeModal(); },
    }, conn.status === "running" ? "Running…" : "Run now"));
  } else {
    const isStreaming = conn.status === "streaming";
    actionsRow.append(el("button", {
      class: "btn small primary", onclick: async () => { await toggleCdc(conn, !isStreaming); closeModal(); },
    }, isStreaming ? "Stop" : "Start"));
  }
  actionsRow.append(el("button", {
    class: "btn small", onclick: async () => { await toggleActive(conn); closeModal(); },
  }, conn.enabled ? "Deactivate" : "Activate"));
  actionsRow.append(el("button", {
    class: "btn small", title: "Reload every table from scratch, ignoring any incremental/xmin cursor",
    onclick: async () => {
      if (!confirm(`Resync all tables for "${conn.name}"? This reloads everything from scratch.`)) return;
      await resyncConnection(conn);
      closeModal();
    },
  }, "↻ Resync all"));
  actionsRow.append(el("button", {
    class: "btn small", onclick: () => openConnectionEditModal(conn),
  }, "Edit"));
  actionsRow.append(el("button", { class: "btn small", onclick: () => viewRuns(conn) }, "Runs"));
  actionsRow.append(el("button", {
    class: "btn small danger", onclick: async () => { await deleteConnection(conn); closeModal(); },
  }, "Delete"));

  const selectedStreams = conn.streams.filter((s) => s.selected !== false);
  const streamRows = conn.streams.length
    ? conn.streams.map((s) => el("div", { class: "stream-row" }, [
        el("span", { class: "name" }, s.schema.name),
        s.selected === false ? el("span", { class: "badge" }, "off") : null,
        s.columns ? el("span", { class: "badge" }, `${s.columns.length}/${s.schema.columns.length} cols`) : null,
        el("span", { class: "badge" }, s.sync_mode),
        el("button", {
          type: "button", class: "btn small",
          disabled: conn.mode === "cdc" ? "true" : null,
          title: conn.mode === "cdc"
            ? "CDC's initial snapshot covers the whole connection — use “Resync all” instead"
            : "Reload just this table from scratch",
          onclick: async () => {
            if (!confirm(`Resync "${s.schema.name}"? This reloads it from scratch.`)) return;
            await resyncConnection(conn, [s.schema.name]);
            closeModal();
          },
        }, "↻"),
      ]))
    : [el("div", { class: "hint" }, "No tables configured.")];

  wrap.append(
    el("h3", {}, conn.name),
    el("div", { class: "detail-icon-row" }, [
      statusBadge(conn.status),
      el("span", { class: `badge ${conn.enabled ? "" : "paused"}` }, conn.enabled ? "active" : "paused"),
      el("span", { class: "badge" }, frequencyLabel),
    ]),
    el("div", { class: "detail-icon-row" }, [
      el("span", { class: "icon" }, typeIcon(source && source.type)),
      source ? source.name : "?",
      el("span", { class: "arrow" }, "→"),
      el("span", { class: "icon" }, typeIcon(destination && destination.type)),
      destination ? destination.name : "?",
    ]),
    conn.status_detail ? el("div", { class: "card-sub", style: "color:var(--err);margin-top:8px" }, conn.status_detail) : null,
    el("div", { class: "card-body", style: "margin-top:16px" }, [
      el("div", { class: "card-stat" }, [el("b", {}, String(selectedStreams.length)), "streams"]),
      el("div", { class: "card-stat" }, [el("b", {}, fmtTime(conn.last_run_at)), "last sync"]),
      el("div", { class: "card-stat" }, [
        el("b", {}, conn.mode === "cdc" ? (conn.status === "streaming" ? "streaming" : "—") : fmtTime(conn.next_run_at)),
        "next run",
      ]),
    ]),
    el("hr", { class: "detail-divider" }),
    actionsRow,
    el("hr", { class: "detail-divider" }),
    el("div", { class: "detail-section-title" }, [
      `Tables (${conn.streams.length})`,
      el("button", {
        type: "button", class: "btn small",
        onclick: () => { closeModal(); openAddSourceModal(conn); },
      }, "+ Add source"),
    ]),
    el("div", { class: "stream-list", style: "max-height:280px" }, streamRows),
    el("div", { class: "modal-actions" }, [
      el("button", { type: "button", class: "btn", onclick: closeModal }, "Close"),
    ])
  );
  openModal(wrap, { wide: true });
}

// ---------------------------------------------------------------------------
// edit an existing connection
// ---------------------------------------------------------------------------
function openConnectionEditModal(conn) {
  const source = state.sources.find((s) => s.id === conn.source_id);
  const wrap = el("div", {});
  const nameInput = el("input", { type: "text", value: conn.name });
  const modeSelect = el("select", {}, [
    el("option", { value: "batch" }, "Batch (scheduled polling)"),
    el("option", { value: "cdc" }, "Real-time (CDC streaming)"),
  ]);
  modeSelect.value = conn.mode;

  const scheduleSelect = el("select", {}, [
    el("option", { value: "interval" }, "Fixed interval"),
    el("option", { value: "cron" }, "Cron expression"),
  ]);
  scheduleSelect.value = conn.schedule_type || "interval";
  const intervalInput = el("input", { type: "number", value: String(conn.interval_seconds || 60), min: "5" });
  const cronInput = el("input", { type: "text", value: conn.cron_expression || "",
    placeholder: "e.g. */5 * * * *  (every 5 minutes)" });
  const prefixInput = el("input", { type: "text", value: conn.table_prefix || "" });
  const namespaceInput = el("input", { type: "text", value: conn.destination_namespace || "" });

  const scheduleField = el("div", { class: "field" }, [el("label", {}, "Schedule type"), scheduleSelect]);
  const intervalField = el("div", { class: "field" }, [el("label", {}, "Poll interval (seconds)"), intervalInput]);
  const cronField = el("div", { class: "field" }, [
    el("label", {}, "Cron expression (UTC, 5-field)"), cronInput,
  ]);

  function updateScheduleVisibility() {
    const isBatch = modeSelect.value === "batch";
    scheduleField.style.display = isBatch ? "block" : "none";
    const isCron = isBatch && scheduleSelect.value === "cron";
    intervalField.style.display = isBatch && !isCron ? "block" : "none";
    cronField.style.display = isCron ? "block" : "none";
  }
  modeSelect.addEventListener("change", updateScheduleVisibility);
  scheduleSelect.addEventListener("change", updateScheduleVisibility);
  updateScheduleVisibility();

  const streamList = el("div", { class: "stream-list" }, "");
  function renderStreamRow(streamCfg) {
    const schema = streamCfg.schema;
    const checkbox = el("input", { type: "checkbox", "data-name": schema.name });
    checkbox.checked = streamCfg.selected !== false;
    const syncModeOptions = [
      el("option", { value: "full_refresh" }, "Full refresh"),
      el("option", { value: "incremental" }, "Incremental (cursor column)"),
    ];
    if (source && source.type === "postgres") {
      syncModeOptions.push(el("option", { value: "xmin" }, "Xmin (system column, no cursor needed)"));
    }
    const syncMode = el("select", { "data-role": "sync_mode" }, syncModeOptions);
    syncMode.value = streamCfg.sync_mode || "full_refresh";
    const cursorSelect = el("select", { "data-role": "cursor_field" }, [
      el("option", { value: "" }, "—"),
      ...schema.columns.map((c) => el("option", { value: c.name }, c.name)),
    ]);
    cursorSelect.value = streamCfg.cursor_field || "";
    cursorSelect.disabled = syncMode.value !== "incremental";
    syncMode.addEventListener("change", () => { cursorSelect.disabled = syncMode.value !== "incremental"; });
    const colsCtl = buildColumnsControl(schema.columns, streamCfg.primary_key || schema.primary_key, streamCfg.columns);
    const row = el("div", { class: "stream-row" }, [
      checkbox,
      el("span", { class: "name" }, `${schema.name}${schema.primary_key.length ? " (pk: " + schema.primary_key.join(",") + ")" : ""}`),
      syncMode, cursorSelect, colsCtl.toggleBtn,
    ]);
    row.dataset.streamName = schema.name;
    row._schema = schema;
    row._primaryKey = streamCfg.primary_key || schema.primary_key;
    row._destTable = streamCfg.destination_table || null;
    row._getSelectedColumns = colsCtl.getSelectedColumns;
    row._columnsPanel = colsCtl.panel;
    return row;
  }
  conn.streams.forEach((st) => {
    const row = renderStreamRow(st);
    streamList.append(row, row._columnsPanel);
  });
  const toolbar = createTableToolbar(streamList);
  toolbar.setVisible(conn.streams.length > 0);

  const addTablesBtn = el("button", { type: "button", class: "btn small" }, "+ Add more tables from source");
  addTablesBtn.addEventListener("click", async () => {
    addTablesBtn.disabled = true;
    addTablesBtn.textContent = "Discovering…";
    try {
      const discovered = await api(`/sources/${conn.source_id}/discover`);
      const existingNames = new Set([...streamList.querySelectorAll(".stream-row")].map((r) => r.dataset.streamName));
      let added = 0;
      for (const schema of discovered) {
        if (existingNames.has(schema.name)) continue;
        const row = renderStreamRow({ schema, selected: false, sync_mode: "full_refresh" });
        row.querySelector('input[type="checkbox"]').checked = false;
        streamList.append(row, row._columnsPanel);
        added++;
      }
      if (added) toolbar.setVisible(true);
      toast(added ? `Found ${added} new table(s) — select the ones you want` : "No new tables found", "ok");
    } catch (e) {
      toast(e.message, "err");
    } finally {
      addTablesBtn.disabled = false;
      addTablesBtn.textContent = "+ Add more tables from source";
    }
  });

  const actions = el("div", { class: "modal-actions" }, [
    el("button", { type: "button", class: "btn", onclick: closeModal }, "Cancel"),
    el("button", { type: "button", class: "btn primary" }, "Save changes"),
  ]);

  actions.lastChild.addEventListener("click", async () => {
    const name = nameInput.value.trim();
    if (!name) return toast("Name is required", "err");

    const rows = [...streamList.querySelectorAll(".stream-row")];
    const streams = [];
    for (const row of rows) {
      const checkbox = row.querySelector('input[type="checkbox"]');
      const syncMode = row.querySelector('[data-role="sync_mode"]').value;
      const cursorField = row.querySelector('[data-role="cursor_field"]').value || null;
      streams.push({
        schema: row._schema, selected: checkbox.checked, sync_mode: syncMode,
        cursor_field: syncMode === "incremental" ? cursorField : null,
        primary_key: row._primaryKey, destination_table: row._destTable,
        columns: row._getSelectedColumns ? row._getSelectedColumns() : null,
      });
    }
    if (!streams.some((s) => s.selected)) return toast("Select at least one table", "err");

    const mode = modeSelect.value;
    const scheduleType = mode === "batch" ? scheduleSelect.value : "interval";
    if (mode === "batch" && scheduleType === "cron" && !cronInput.value.trim()) {
      return toast("Cron expression is required", "err");
    }
    // PATCH itself stops/restarts the CDC worker when mode actually changes
    // (see patch_connection on the backend); explicitly re-enabling here
    // guarantees the connection is left active after the edit either way.
    try {
      await api(`/connections/${conn.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          name, mode, schedule_type: scheduleType, enabled: true,
          interval_seconds: Number(intervalInput.value) || 60,
          cron_expression: scheduleType === "cron" ? cronInput.value.trim() : null,
          destination_namespace: namespaceInput.value.trim() || null,
          table_prefix: prefixInput.value.trim(), streams,
        }),
      });
      if (mode === "cdc") {
        // A running CDC worker only reads streams/config once at startup, so
        // cycle it to pick up whatever just changed (selection, sync mode, …).
        await api(`/connections/${conn.id}/stop`, { method: "POST" });
        await api(`/connections/${conn.id}/start`, { method: "POST" });
      }
      toast("Connection updated", "ok");
      closeModal();
      await refreshAll();
    } catch (e) {
      toast(e.message, "err");
    }
  });

  wrap.append(
    el("h3", {}, `Edit connection — ${conn.name}`),
    el("div", { class: "form-grid" }, [
      el("div", { class: "field span2" }, [el("label", {}, "Name *"), nameInput]),
      el("div", { class: "field" }, [el("label", {}, "Sync mode"), modeSelect]),
      scheduleField,
      intervalField,
      cronField,
      el("div", { class: "field" }, [el("label", {}, "Destination table prefix"), prefixInput]),
      el("div", { class: "field" }, [el("label", {}, "Destination namespace/schema"), namespaceInput]),
    ]),
    el("div", { class: "hint" }, "Source and destination can't be changed after creation — delete and recreate the connection to point it elsewhere."),
    addTablesBtn, toolbar.node, streamList, actions
  );
  openModal(wrap, { wide: true });
}

// ---------------------------------------------------------------------------
// reusable multi-source table picker (used by the create wizard and by
// "+ Add source" inside a connection's detail view)
// ---------------------------------------------------------------------------
// Search/filter, select-all/none, and a bulk update-method setter for a
// `.stream-list` of `.stream-row` elements — configuring dozens of tables
// one dropdown at a time doesn't scale, so let people filter down and apply
// a choice to everything at once. Shared by the create wizard and the edit
// modal's table list.
// A per-table "which columns?" control: a small toggle button that expands
// a checkbox grid of that table's columns. Primary-key columns are locked
// on (needed for upsert/delete matching, and for incremental cursor
// tracking). Returns null (meaning "sync everything") unless the user
// actually unchecked something, so the common case stays a no-op.
function buildColumnsControl(columns, primaryKey, initialSelected) {
  const pkSet = new Set(primaryKey || []);
  const checks = columns.map((c) => {
    const isPk = pkSet.has(c.name);
    const cb = el("input", { type: "checkbox" });
    cb.checked = initialSelected ? initialSelected.includes(c.name) : true;
    if (isPk) { cb.checked = true; cb.disabled = true; }
    return { name: c.name, type: c.type, cb, isPk };
  });

  const toggleBtn = el("button", { type: "button", class: "btn small columns-toggle" }, "");
  const panel = el("div", { class: "columns-panel", hidden: "true" },
    checks.map(({ name, type, cb, isPk }) =>
      el("label", { class: `column-chip${isPk ? " locked" : ""}`, title: isPk ? "Part of the primary key — always included" : type }, [
        cb, name, isPk ? el("span", { class: "pk-tag" }, "pk") : null,
      ])));
  toggleBtn.addEventListener("click", () => { panel.hidden = !panel.hidden; });

  function updateLabel() {
    const selected = checks.filter((c) => c.cb.checked).length;
    toggleBtn.textContent = selected === checks.length
      ? `Columns (all ${checks.length})`
      : `Columns (${selected}/${checks.length})`;
  }
  checks.forEach(({ cb }) => cb.addEventListener("change", updateLabel));
  updateLabel();

  function getSelectedColumns() {
    const selected = checks.filter((c) => c.cb.checked).map((c) => c.name);
    return selected.length === checks.length ? null : selected;
  }

  return { toggleBtn, panel, getSelectedColumns };
}

function createTableToolbar(streamList) {
  const searchInput = el("input", { type: "text", placeholder: "Filter tables by name…" });
  const selectAllBtn = el("button", { type: "button", class: "btn small" }, "Select all");
  const selectNoneBtn = el("button", { type: "button", class: "btn small" }, "Select none");
  const bulkModeSelect = el("select", {}, [
    el("option", { value: "" }, "Set update method for selected…"),
    el("option", { value: "full_refresh" }, "Full refresh"),
    el("option", { value: "incremental" }, "Incremental (cursor column)"),
    el("option", { value: "xmin" }, "Xmin (Postgres only)"),
  ]);
  const applyBulkBtn = el("button", { type: "button", class: "btn small" }, "Apply");
  const toolbar = el("div", { class: "picker-toolbar", hidden: "true" }, [
    searchInput,
    el("div", { class: "picker-toolbar-group" }, [selectAllBtn, selectNoneBtn]),
    el("div", { class: "picker-toolbar-group" }, [bulkModeSelect, applyBulkBtn]),
  ]);

  function visibleRows() {
    return [...streamList.querySelectorAll(".stream-row")].filter((r) => !r.hidden);
  }
  searchInput.addEventListener("input", () => {
    const q = searchInput.value.trim().toLowerCase();
    for (const row of streamList.querySelectorAll(".stream-row")) {
      row.hidden = !!q && !row.dataset.streamName.toLowerCase().includes(q);
    }
  });
  selectAllBtn.addEventListener("click", () => {
    for (const row of visibleRows()) row.querySelector('input[type="checkbox"]').checked = true;
  });
  selectNoneBtn.addEventListener("click", () => {
    for (const row of visibleRows()) row.querySelector('input[type="checkbox"]').checked = false;
  });
  applyBulkBtn.addEventListener("click", () => {
    const mode = bulkModeSelect.value;
    if (!mode) return;
    let applied = 0, skipped = 0;
    for (const row of visibleRows()) {
      const checkbox = row.querySelector('input[type="checkbox"]');
      if (!checkbox.checked) continue;
      const syncSelect = row.querySelector('[data-role="sync_mode"]');
      if ([...syncSelect.options].some((o) => o.value === mode)) {
        syncSelect.value = mode;
        syncSelect.dispatchEvent(new Event("change"));
        applied++;
      } else {
        skipped++;
      }
    }
    toast(`Applied to ${applied} table(s)` + (skipped ? ` — skipped ${skipped} (unsupported for that source)` : ""), "ok");
  });

  return {
    node: toolbar,
    searchInput,
    setVisible(visible) { toolbar.hidden = !visible; },
    reset() { searchInput.value = ""; },
  };
}

function createSourcePicker(sources) {
  const sourceCheckboxes = sources.map((s) => {
    const cb = el("input", { type: "checkbox", value: s.id });
    if (sources.length === 1) cb.checked = true;
    return { source: s, checkbox: cb };
  });
  const sourcesWrap = el("div", { class: "stream-list" },
    sourceCheckboxes.map(({ source, checkbox }) =>
      el("label", { class: "stream-row" }, [
        checkbox,
        el("span", { class: "icon" }, typeIcon(source.type)),
        el("span", { class: "name" }, `${source.name} (${source.type})`),
      ])));

  const discoverBtn = el("button", { type: "button", class: "btn" }, "Discover tables");
  const refreshBtn = el("button", { type: "button", class: "btn small", title: "Bypass the cached schema and re-scan every checked source" }, "↻ Refresh");
  const discoverRow = el("div", { style: "display:flex; gap:8px; align-items:center" }, [discoverBtn, refreshBtn]);

  const streamList = el("div", { class: "stream-list" }, el("div", { class: "hint" }, "Check one or more sources above, then click Discover."));
  const toolbar = createTableToolbar(streamList);

  let discoveredBySource = {}; // sourceId -> streams[]

  function getCheckedSources() {
    return sourceCheckboxes.filter((x) => x.checkbox.checked).map((x) => x.source);
  }

  async function runDiscover(refresh) {
    const checked = getCheckedSources();
    if (!checked.length) return toast("Check at least one source first", "err");
    streamList.innerHTML = "";
    streamList.append(el("div", { class: "hint" }, "Discovering…"));
    try {
      discoveredBySource = {};
      for (const source of checked) {
        discoveredBySource[source.id] = await api(`/sources/${source.id}/discover${refresh ? "?refresh=true" : ""}`);
      }
      renderStreams(checked);
    } catch (e) {
      streamList.innerHTML = "";
      streamList.append(el("div", { class: "hint" }, e.message));
    }
  }
  discoverBtn.addEventListener("click", () => runDiscover(false));
  refreshBtn.addEventListener("click", () => runDiscover(true));

  function renderStreams(pickedSources) {
    streamList.innerHTML = "";
    toolbar.reset();
    let any = false;
    for (const source of pickedSources) {
      const streams = discoveredBySource[source.id] || [];
      if (!streams.length) continue;
      any = true;
      if (pickedSources.length > 1) {
        streamList.append(el("div", { class: "card-sub", style: "margin-top:10px;font-weight:600" },
          `${typeIcon(source.type)} ${source.name} (${source.type})`));
      }
      for (const stream of streams) {
        const checkbox = el("input", { type: "checkbox", checked: "true", "data-name": stream.name });
        const syncModeOptions = [
          el("option", { value: "full_refresh" }, "Full refresh"),
          el("option", { value: "incremental" }, "Incremental (cursor column)"),
        ];
        if (source.type === "postgres") {
          syncModeOptions.push(el("option", { value: "xmin" }, "Xmin (system column, no cursor needed)"));
        }
        const syncMode = el("select", { "data-role": "sync_mode" }, syncModeOptions);
        const cursorSelect = el("select", { "data-role": "cursor_field" }, [
          el("option", { value: "" }, "—"),
          ...stream.columns.map((c) => el("option", { value: c.name }, c.name)),
        ]);
        cursorSelect.disabled = true;
        syncMode.addEventListener("change", () => { cursorSelect.disabled = syncMode.value !== "incremental"; });
        const colsCtl = buildColumnsControl(stream.columns, stream.primary_key);
        const row = el("div", { class: "stream-row" }, [
          checkbox,
          el("span", { class: "name" }, `${stream.name}${stream.primary_key.length ? " (pk: " + stream.primary_key.join(",") + ")" : ""}`),
          syncMode, cursorSelect, colsCtl.toggleBtn,
        ]);
        row.dataset.streamName = stream.name;
        row.dataset.sourceId = source.id;
        row._getSelectedColumns = colsCtl.getSelectedColumns;
        streamList.append(row, colsCtl.panel);
      }
    }
    if (!any) streamList.append(el("div", { class: "hint" }, "No tables found."));
    toolbar.setVisible(any);
  }

  function getStreamsBySource() {
    const rows = [...streamList.querySelectorAll(".stream-row")];
    const streamsBySource = {};
    for (const row of rows) {
      const checkbox = row.querySelector('input[type="checkbox"]');
      if (!checkbox.checked) continue;
      const sourceId = row.dataset.sourceId;
      const streamName = row.dataset.streamName;
      const schema = (discoveredBySource[sourceId] || []).find((d) => d.name === streamName);
      if (!schema) continue;
      const syncMode = row.querySelector('[data-role="sync_mode"]').value;
      const cursorField = row.querySelector('[data-role="cursor_field"]').value || null;
      (streamsBySource[sourceId] = streamsBySource[sourceId] || []).push({
        schema, selected: true, sync_mode: syncMode,
        cursor_field: syncMode === "incremental" ? cursorField : null,
        primary_key: schema.primary_key, destination_table: null,
        columns: row._getSelectedColumns ? row._getSelectedColumns() : null,
      });
    }
    return streamsBySource;
  }

  return { sourcesWrap, discoverRow, pickerToolbar: toolbar.node, streamList, getCheckedSources, getStreamsBySource };
}

/** Creates one connection per sourceId in `streamsBySource`, all sharing the
 * given settings. Returns { created: string[], failed: string[] }.
 * `forceSuffix` always appends "— <source>" to the name even for a single
 * source — required when `baseName` is already taken by another connection
 * (e.g. adding a source to an existing one), not just when there are
 * multiple sources to disambiguate between. */
async function fanOutConnections(baseName, streamsBySource, sources, settings, forceSuffix = false) {
  const sourceIds = Object.keys(streamsBySource);
  const multi = forceSuffix || sourceIds.length > 1;
  const created = [];
  const failed = [];
  for (const sourceId of sourceIds) {
    const source = sources.find((s) => s.id === sourceId);
    const connName = multi ? `${baseName} — ${source.name}` : baseName;
    try {
      const conn = await api("/connections", {
        method: "POST",
        body: JSON.stringify({
          name: connName, source_id: sourceId, streams: streamsBySource[sourceId], ...settings,
        }),
      });
      if (settings.mode === "cdc") await api(`/connections/${conn.id}/start`, { method: "POST" });
      created.push(connName);
    } catch (e) {
      failed.push(`${connName}: ${e.message}`);
    }
  }
  return { created, failed };
}

// ---------------------------------------------------------------------------
// new connection wizard
// ---------------------------------------------------------------------------
function openConnectionModal() {
  if (!state.sources.length || !state.destinations.length) {
    return toast("Create at least one source and one destination first", "err");
  }
  const wrap = el("div", {});
  const nameInput = el("input", { type: "text", name: "_name" });

  // One connection = one source (CDC's replication slot/binlog position is
  // inherently tied to a single database), so picking several sources here
  // fans out into one connection per source, all pointed at the same
  // destination and created together in one go.
  const picker = createSourcePicker(state.sources);
  const destSelect = el("select", {}, state.destinations.map((d) => el("option", { value: d.id }, `${typeIcon(d.type)} ${d.name} (${d.type})`)));
  const modeSelect = el("select", {}, [
    el("option", { value: "batch" }, "Batch (scheduled polling)"),
    el("option", { value: "cdc" }, "Real-time (CDC streaming)"),
  ]);
  const scheduleSelect = el("select", {}, [
    el("option", { value: "interval" }, "Fixed interval"),
    el("option", { value: "cron" }, "Cron expression"),
  ]);
  const intervalInput = el("input", { type: "number", value: "60", min: "5" });
  const cronInput = el("input", { type: "text", placeholder: "e.g. */5 * * * *  (every 5 minutes)" });
  const prefixInput = el("input", { type: "text", placeholder: "e.g. src_" });
  const namespaceInput = el("input", { type: "text", placeholder: "leave blank to mirror source schema" });

  const scheduleField = el("div", { class: "field" }, [el("label", {}, "Schedule type"), scheduleSelect]);
  const intervalField = el("div", { class: "field" }, [el("label", {}, "Poll interval (seconds)"), intervalInput]);
  const cronField = el("div", { class: "field" }, [
    el("label", {}, "Cron expression (UTC, 5-field)"), cronInput,
  ]);
  cronField.style.display = "none";

  function updateScheduleVisibility() {
    const isBatch = modeSelect.value === "batch";
    scheduleField.style.display = isBatch ? "block" : "none";
    const isCron = isBatch && scheduleSelect.value === "cron";
    intervalField.style.display = isBatch && !isCron ? "block" : "none";
    cronField.style.display = isCron ? "block" : "none";
  }
  modeSelect.addEventListener("change", updateScheduleVisibility);
  scheduleSelect.addEventListener("change", updateScheduleVisibility);
  updateScheduleVisibility();

  const actions = el("div", { class: "modal-actions" }, [
    el("button", { type: "button", class: "btn", onclick: closeModal }, "Cancel"),
    el("button", { type: "button", class: "btn primary" }, "Create connection"),
  ]);

  actions.lastChild.addEventListener("click", async () => {
    const name = nameInput.value.trim();
    if (!name) return toast("Name is required", "err");
    if (!picker.getCheckedSources().length) return toast("Check at least one source", "err");

    const streamsBySource = picker.getStreamsBySource();
    if (!Object.keys(streamsBySource).length) return toast("Discover and select at least one table", "err");

    const mode = modeSelect.value;
    const scheduleType = mode === "batch" ? scheduleSelect.value : "interval";
    if (mode === "batch" && scheduleType === "cron" && !cronInput.value.trim()) {
      return toast("Cron expression is required", "err");
    }

    const { created, failed } = await fanOutConnections(name, streamsBySource, state.sources, {
      destination_id: destSelect.value, mode, schedule_type: scheduleType,
      interval_seconds: Number(intervalInput.value) || 60,
      cron_expression: scheduleType === "cron" ? cronInput.value.trim() : null,
      destination_namespace: namespaceInput.value.trim() || null,
      table_prefix: prefixInput.value.trim(),
    });

    if (created.length) toast(`Created ${created.length} connection(s)`, "ok");
    if (failed.length) toast(failed.join(" | "), "err");
    if (created.length) closeModal();
    await refreshAll();
  });

  wrap.append(
    el("h3", {}, "New connection"),
    el("div", { class: "form-grid" }, [
      el("div", { class: "field span2" }, [el("label", {}, "Name *"), nameInput]),
      el("div", { class: "field span2" }, [
        el("label", {}, `Source(s) *${state.sources.length > 1 ? " — check one or more" : ""}`),
        picker.sourcesWrap,
      ]),
      el("div", { class: "field" }, [el("label", {}, "Destination"), destSelect]),
      el("div", { class: "field" }, [el("label", {}, "Sync mode"), modeSelect]),
      scheduleField,
      intervalField,
      cronField,
      el("div", { class: "field" }, [el("label", {}, "Destination table prefix"), prefixInput]),
      el("div", { class: "field" }, [el("label", {}, "Destination namespace/schema"), namespaceInput]),
    ]),
    picker.discoverRow, picker.pickerToolbar, picker.streamList, actions
  );
  openModal(wrap);
}

document.getElementById("newConnectionBtn").addEventListener("click", openConnectionModal);

// ---------------------------------------------------------------------------
// add another source to an existing connection's destination
// ---------------------------------------------------------------------------
function baseConnectionName(name) {
  const idx = name.lastIndexOf(" — ");
  return idx === -1 ? name : name.slice(0, idx);
}

function openAddSourceModal(baseConn) {
  const destination = state.destinations.find((d) => d.id === baseConn.destination_id);
  const otherSources = state.sources.filter((s) => s.id !== baseConn.source_id);
  if (!otherSources.length) {
    return toast("No other sources available — create one first", "err");
  }
  const wrap = el("div", {});
  const nameInput = el("input", { type: "text", value: baseConnectionName(baseConn.name) });
  const picker = createSourcePicker(otherSources);

  const actions = el("div", { class: "modal-actions" }, [
    el("button", { type: "button", class: "btn", onclick: closeModal }, "Cancel"),
    el("button", { type: "button", class: "btn primary" }, "Add source(s)"),
  ]);

  actions.lastChild.addEventListener("click", async () => {
    const name = nameInput.value.trim();
    if (!name) return toast("Name is required", "err");
    if (!picker.getCheckedSources().length) return toast("Check at least one source", "err");

    const streamsBySource = picker.getStreamsBySource();
    if (!Object.keys(streamsBySource).length) return toast("Discover and select at least one table", "err");

    const { created, failed } = await fanOutConnections(name, streamsBySource, otherSources, {
      destination_id: baseConn.destination_id, mode: baseConn.mode,
      schedule_type: baseConn.schedule_type, interval_seconds: baseConn.interval_seconds,
      cron_expression: baseConn.cron_expression,
      destination_namespace: baseConn.destination_namespace, table_prefix: baseConn.table_prefix,
    }, /* forceSuffix */ true);

    if (created.length) toast(`Created ${created.length} connection(s)`, "ok");
    if (failed.length) toast(failed.join(" | "), "err");
    if (created.length) closeModal();
    await refreshAll();
  });

  wrap.append(
    el("h3", {}, "Add source"),
    el("div", { class: "hint" },
      `New connection(s) will target "${destination.name}" using the same mode (${baseConn.mode}) and schedule as "${baseConn.name}".`),
    el("div", { class: "form-grid" }, [
      el("div", { class: "field span2" }, [el("label", {}, "Name *"), nameInput]),
      el("div", { class: "field span2" }, [
        el("label", {}, "Source(s) * — check one or more"),
        picker.sourcesWrap,
      ]),
    ]),
    picker.discoverRow, picker.pickerToolbar, picker.streamList, actions
  );
  openModal(wrap);
}

// ---------------------------------------------------------------------------
// rendering
// ---------------------------------------------------------------------------
function renderList(containerId, items, renderer, emptyText) {
  const container = document.getElementById(containerId);
  container.innerHTML = "";
  if (!items.length) {
    container.append(el("div", { class: "empty" }, emptyText));
    return;
  }
  items.forEach((item) => container.append(renderer(item)));
}

function renderAll() {
  renderList("sourcesList", state.sources, (s) => connectorCard(s, "source"), "No sources yet.");
  renderList("destinationsList", state.destinations, (d) => connectorCard(d, "destination"), "No destinations yet.");
  renderList("connectionsList", state.connections, connectionCard, "No connections yet. Create a source and destination, then add a connection.");
}

async function refreshConnections() {
  state.connections = await api("/connections");
  renderList("connectionsList", state.connections, connectionCard, "No connections yet. Create a source and destination, then add a connection.");
}

async function refreshAll() {
  const [sources, destinations, connections, types] = await Promise.all([
    api("/sources"), api("/destinations"), api("/connections"), api("/connector-types"),
  ]);
  state.sources = sources;
  state.destinations = destinations;
  state.connections = connections;
  state.connectorTypes = types;
  renderAll();
}

// ---------------------------------------------------------------------------
// live log console + websocket
// ---------------------------------------------------------------------------
const logConsole = document.getElementById("logConsole");
document.getElementById("clearLogsBtn").addEventListener("click", () => { logConsole.innerHTML = ""; });

function appendLog(evt) {
  const line = el("div", { class: "log-line" }, [
    el("span", { class: "ts" }, new Date(evt.ts).toLocaleTimeString() + "  "),
    el("span", { class: "lvl-" + (evt.level || "INFO") }, `[${evt.level || "INFO"}] `),
    evt.connection_id ? `(${evt.connection_id.slice(0, 8)}) ` : "",
    evt.message || "",
  ]);
  logConsole.appendChild(line);
  const nearBottom = logConsole.scrollHeight - logConsole.scrollTop - logConsole.clientHeight < 80;
  if (nearBottom) logConsole.scrollTop = logConsole.scrollHeight;
}

function updateLiveStat(connectionId, read, written) {
  const readNode = document.querySelector(`#stat-read-${connectionId} b`);
  const writtenNode = document.querySelector(`#stat-written-${connectionId} b`);
  if (readNode && read !== undefined) readNode.textContent = read;
  if (writtenNode && written !== undefined) writtenNode.textContent = written;
}

function connectWebSocket() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/api/ws`);
  const statusEl = document.getElementById("wsStatus");

  ws.onopen = () => { statusEl.textContent = "live"; statusEl.className = "ws-status live"; };
  ws.onclose = () => {
    statusEl.textContent = "reconnecting…";
    statusEl.className = "ws-status down";
    setTimeout(connectWebSocket, 2000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (msg) => {
    let evt;
    try { evt = JSON.parse(msg.data); } catch { return; }
    if (evt.type === "log") {
      appendLog(evt);
    } else if (evt.type === "run_progress") {
      updateLiveStat(evt.connection_id, evt.read, evt.written);
    } else if (evt.type === "run_started" || evt.type === "run_finished") {
      refreshConnections();
    }
  };
}

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------
refreshAll().catch((e) => toast(e.message, "err"));
connectWebSocket();
setInterval(() => { refreshConnections().catch(() => {}); }, 15000);
