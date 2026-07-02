// app.js — the runspec room browser client (vanilla, no build step).
//
// The product is the help desk. One room = one helpdesk = one queue, so the
// visitor never picks a queue — the server tells us the single queue at /me and
// derives a private thread from it + the visitor's identity (/ws?queue=&user=).
// The aside shows the queue's live catalogue (the runnables the on-shift operator
// consoles publish); picking one renders a form from its inputSchema, and
// submitting sends a "request" frame the hub auto-assigns to an operator. Each
// request shows its claim-free lifecycle (assigned → in progress → done/failed)
// inline; when no operator is on shift it shows "no operator available" with a
// Page-the-team button.
//
// Identity. A verified server (email-verify sign-up) supplies the name + team in
// the session — /me returns them and the join fields lock. In dev mode the browser
// declares both (?user= / ?team= on the link, or typed in the form), the same way
// dev mode has always declared the name. A ?room= deep-link still opens a legacy
// group-chat room. See docs/design/runnables-portal.md §3.

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const join = $("join");
  const app = $("app");
  const transcript = $("transcript");
  const presenceList = $("presence");
  const statusEl = $("status");
  const titleEl = $("title");
  const catalogPanel = $("catalog-panel");
  const catalogList = $("catalog");
  const catalogSearch = $("catalog-search");
  const reqForm = $("reqform");

  let ws = null;
  let me = "";
  let team = ""; // the visitor's team (department)
  let room = ""; // legacy group-chat room (?room= deep-link)
  let queue = ""; // the single helpdesk queue this room serves (from /me)
  let authed = false; // verified server (email-verify) — name + team are locked
  let operator = false; // operator session — sees the full catalogue + runbooks
  let catalog = []; // the queue's published tools (and runbooks, for operators)
  const present = new Map(); // user -> status
  const requests = new Map(); // request_id -> { status node } for live lifecycle

  // Ask the server who we are and which queue this room serves. In a verified
  // server the name + team come from the session, so there's nothing for the
  // visitor to fill in — we join straight away (no gate). In dev mode the visitor
  // declares them in the form (or on the link) and clicks Join.
  // White-label look from /me (config's [branding]). accent_color recolours the
  // --accent CSS variable; brand_name renames the browser tab + join heading. Both
  // optional — an unbranded server sends {} and we keep the defaults. (The favicon
  // is served at /favicon.ico, so the static <link> already picks it up.)
  function applyBranding(b) {
    if (!b || typeof b !== "object") return;
    if (b.accent_color) {
      document.documentElement.style.setProperty("--accent", b.accent_color);
    }
    if (b.brand_name) {
      document.title = b.brand_name;
      const h1 = join.querySelector("h1");
      if (h1) h1.textContent = b.brand_name;
    }
  }

  async function whoami() {
    try {
      const info = await (await fetch("/me")).json();
      authed = !!info.auth;
      operator = !!info.operator; // elevated view: full catalogue + runbooks
      applyBranding(info.branding); // white-label before the join form shows
      if (info.queue) queue = info.queue; // the single helpdesk queue
      if (authed) {
        if (info.name || info.user) $("user").value = info.name || info.user;
        if (info.team) $("team").value = info.team;
        doJoin(); // verified — skip the join form entirely
      }
    } catch (_e) {
      /* dev fallback: leave the name + team fields editable */
    }
  }
  whoami();

  // Adapt the join form to the link. The help desk is the product, so by default
  // the visitor just joins the single queue. Dev-mode identity rides the link
  // (?user=alice&team=sales); a ?room= deep-link still opens a legacy group-chat
  // room (and re-reveals the Room field for that one visit).
  const params = new URLSearchParams(location.search);
  const urlRoom = (params.get("room") || "").trim();
  const urlUser = (params.get("user") || "").trim();
  const urlTeam = (params.get("team") || "").trim();
  if (urlUser) $("user").value = urlUser;
  if (urlTeam) $("team").value = urlTeam;
  if (urlRoom) {
    $("room").value = urlRoom;
    $("room-label").hidden = false; // legacy group-chat deep-link
    $("team-label").hidden = true;
    $("mode-hint").hidden = true;
  }

  function wsUrl() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    let q;
    if (room) {
      q = new URLSearchParams({ room, user: me });
    } else {
      // Helpdesk: the server derives the thread from queue + user. The team is a
      // dev-mode declaration here; a verified server ignores it and uses the
      // session's team (so it can't be spoofed).
      q = new URLSearchParams({ queue, user: me });
      if (team) q.set("team", team);
    }
    return `${proto}//${location.host}/ws?${q.toString()}`;
  }

  function setStatus(text, cls) {
    statusEl.textContent = text;
    statusEl.className = "status " + (cls || "");
  }

  function fmtTime(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function addMessage(frame) {
    const li = document.createElement("li");
    li.className =
      "msg" +
      (frame.sender === me ? " mine" : "") +
      (frame.kind === "request" ? " req" : "");
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = frame.sender;
    const body = document.createElement("span");
    body.className = "text";
    body.textContent = frame.text;
    const time = document.createElement("span");
    time.className = "time";
    time.textContent = fmtTime(frame.ts);
    li.append(who, body, time);
    // A portal request carries a lifecycle: attach an (initially empty) status row
    // keyed by its id, which request_status frames fill in. On a reload from
    // backscroll there's no live status (it isn't persisted) — the row stays empty.
    const rid = frame.kind === "request" && frame.data && frame.data.request_id;
    if (rid) {
      const status = document.createElement("div");
      status.className = "reqstatus";
      li.append(status);
      requests.set(rid, { el: status });
    }
    transcript.append(li);
    transcript.scrollTop = transcript.scrollHeight;
  }

  // Render one request's claim-free lifecycle into its inline status row. When a
  // request is unassigned (no on-shift operator) it shows "no operator available"
  // plus a Page-the-team button that re-chimes every connected operator console.
  function setReqStatus(frame) {
    const rec = requests.get(frame.id);
    if (!rec) return; // status for a request not in this view (e.g. pre-reload)
    rec.el.innerHTML = "";
    const badge = document.createElement("span");
    badge.className = "reqbadge " + (frame.state || "");
    badge.textContent = reqLabel(frame);
    rec.el.append(badge);
    if (frame.state === "unassigned") {
      const note = document.createElement("span");
      note.className = "reqnote";
      note.textContent = "No operator on shift — IM/call them, or";
      const pageBtn = document.createElement("button");
      pageBtn.type = "button";
      pageBtn.className = "page-btn";
      pageBtn.textContent = "Page the team";
      pageBtn.addEventListener("click", () => {
        if (ws && ws.readyState === WebSocket.OPEN)
          ws.send(JSON.stringify({ type: "page", id: frame.id }));
        pageBtn.textContent = "Paged ✓";
        pageBtn.disabled = true;
        setTimeout(() => {
          pageBtn.textContent = "Page the team";
          pageBtn.disabled = false;
        }, 3000);
      });
      rec.el.append(note, pageBtn);
    }
    if (frame.error) {
      const err = document.createElement("span");
      err.className = "reqerr";
      err.textContent = frame.error;
      rec.el.append(err);
    }
  }

  function reqLabel(frame) {
    switch (frame.state) {
      case "unassigned":
        return "no operator available";
      case "assigned":
        return frame.assignee ? `assigned · ${frame.assignee}` : "assigned";
      case "in_progress":
        return "in progress…";
      case "done":
        return "done ✓";
      case "failed":
        return "failed";
      default:
        return frame.state || "";
    }
  }

  function renderPresence() {
    presenceList.innerHTML = "";
    for (const [user, status] of [...present.entries()].sort()) {
      const li = document.createElement("li");
      li.className = "p " + status;
      li.textContent = user;
      presenceList.append(li);
    }
  }

  function onFrame(frame) {
    if (frame.type === "message") {
      addMessage(frame);
    } else if (frame.type === "presence") {
      if (frame.status === "away") present.delete(frame.user);
      else present.set(frame.user, frame.status);
      renderPresence();
    } else if (frame.type === "request_status") {
      setReqStatus(frame);
    }
  }

  // ── self-service catalog (helpdesk mode) ──────────────────────────────────

  async function loadCatalog() {
    try {
      const r = await fetch(`/catalog?queue=${encodeURIComponent(queue)}`);
      catalog = r.ok ? (await r.json()).tools || [] : [];
    } catch (_e) {
      catalog = [];
    }
    renderCatalog();
  }

  // The label shown for a catalog entry: a runbook carries a human x-title (its
  // wire name is the collision-proof `skill:<id>`); a runnable is its bare name.
  function toolLabel(tool) {
    return tool["x-kind"] === "skill" ? tool["x-title"] || tool.name : tool.name;
  }

  function appendCatalogItem(tool) {
    const li = document.createElement("li");
    li.className = "cat-item";
    const name = document.createElement("span");
    name.className = "cat-name";
    name.textContent = toolLabel(tool);
    const desc = document.createElement("span");
    desc.className = "cat-desc";
    desc.textContent = tool.description || "";
    li.append(name, desc);
    li.addEventListener("click", () => renderForm(tool));
    catalogList.append(li);
  }

  function appendCatalogHeading(text) {
    const li = document.createElement("li");
    li.className = "cat-heading";
    li.textContent = text;
    catalogList.append(li);
  }

  function appendCatalogNote(text) {
    const li = document.createElement("li");
    li.className = "cat-empty";
    li.textContent = text;
    catalogList.append(li);
  }

  // Free-text filter over an entry's label, description, and (for a fleet runnable)
  // its hosts — the same fields shown in the list, so what you read is what you match.
  function catalogMatches(tool, q) {
    if (!q) return true;
    const hay = [
      toolLabel(tool),
      tool.description || "",
      (tool["x-hosts"] || []).join(" "),
    ]
      .join(" ")
      .toLowerCase();
    return hay.includes(q);
  }

  function renderCatalog() {
    catalogPanel.hidden = false;
    reqForm.hidden = true;
    catalogList.hidden = false;
    catalogList.innerHTML = "";
    // The search box is only useful (and only shown) when there's a catalogue.
    catalogSearch.hidden = !catalog.length;
    if (!catalog.length) {
      appendCatalogNote("Nothing available — no operator may be on shift.");
      return;
    }
    const q = (catalogSearch.value || "").trim().toLowerCase();
    // Runnables first, then a Runbooks section. A heading is shown only when both
    // kinds are present, so a visitor's plain list is unchanged.
    const runnables = catalog.filter(
      (t) => t["x-kind"] !== "skill" && catalogMatches(t, q),
    );
    const skills = catalog.filter(
      (t) => t["x-kind"] === "skill" && catalogMatches(t, q),
    );
    if (!runnables.length && !skills.length) {
      appendCatalogNote(`No matches for “${q}”.`);
      return;
    }
    if (runnables.length && skills.length) appendCatalogHeading("Runnables");
    for (const tool of runnables) appendCatalogItem(tool);
    if (skills.length) {
      appendCatalogHeading("Runbooks");
      for (const tool of skills) appendCatalogItem(tool);
    }
  }

  // Re-filter as the operator types. The value persists across catalogue refreshes
  // (the poll re-renders but never clears the box).
  catalogSearch.addEventListener("input", renderCatalog);

  // Generate a form from the tool's inputSchema (JSON Schema): enum → select,
  // boolean → checkbox, integer/number → number input, anything else → text.
  // This is UX-level validation only — runspec itself validates authoritatively
  // when the agent console enacts the request.
  function renderForm(tool) {
    // The form is a centered modal over a dimmed backdrop, so the catalogue grid
    // stays in place behind it (no reflow) — just reveal the form.
    reqForm.hidden = false;
    reqForm.innerHTML = "";

    const h = document.createElement("h3");
    h.textContent = toolLabel(tool);
    reqForm.append(h);
    if (tool.description) {
      const p = document.createElement("p");
      p.className = "cat-desc";
      p.textContent = tool.description;
      reqForm.append(p);
    }

    // Operators can see — and, on a fleet, pick — which host a runnable lands on
    // (e.g. "check disk space" on a specific box). >1 hosts → a required picker;
    // exactly 1 → a caption so they still see where it runs. Runbooks run on the
    // console (they target hosts from inside the runbook), so they carry no host.
    const hosts =
      tool["x-kind"] !== "skill" && Array.isArray(tool["x-hosts"])
        ? tool["x-hosts"]
        : [];
    if (operator && hosts.length > 1) {
      const label = document.createElement("label");
      const caption = document.createElement("span");
      caption.textContent = "Host *";
      label.append(caption);
      const sel = document.createElement("select");
      for (const hn of hosts) {
        const o = document.createElement("option");
        o.value = o.textContent = String(hn);
        sel.append(o);
      }
      sel.dataset.host = "1";
      label.append(sel);
      reqForm.append(label);
    } else if (operator && hosts.length === 1) {
      const cap = document.createElement("p");
      cap.className = "cat-desc";
      cap.textContent = `Runs on ${hosts[0]}`;
      reqForm.append(cap);
    }

    const schema = tool.inputSchema || {};
    const props = schema.properties || {};
    const required = new Set(schema.required || []);

    // One schema property → a labelled control. Mirrors the desktop console's
    // Forms tab: native date/datetime pickers for x-ui="date"/"datetime" on a
    // string arg, and auto-growing textareas for plain strings (Shift+Enter for a
    // newline, plain Enter suppressed). Keeps data-arg/data-argType so the submit
    // handler is unchanged.
    function buildField(name, prop) {
      const label = document.createElement("label");
      const caption = document.createElement("span");
      caption.textContent = name + (required.has(name) ? " *" : "");
      label.append(caption);

      const xui = prop["x-ui"];
      let input;
      if (Array.isArray(prop.enum)) {
        input = document.createElement("select");
        for (const opt of prop.enum) {
          const o = document.createElement("option");
          o.value = o.textContent = String(opt);
          input.append(o);
        }
        if (prop.default !== undefined) input.value = String(prop.default);
      } else if (prop.type === "boolean") {
        input = document.createElement("input");
        input.type = "checkbox";
        input.checked = !!prop.default;
      } else if (xui === "date" || xui === "datetime") {
        input = document.createElement("input");
        input.type = xui === "datetime" ? "datetime-local" : "date";
        if (prop.default !== undefined) input.value = String(prop.default);
        if (required.has(name)) input.required = true;
        // The native datetime-local control yields a zoneless local string
        // (YYYY-MM-DDTHH:MM); flag it so submit normalizes to a UTC ISO string
        // with Z, matching the desktop console's DatePicker output. A plain date
        // (YYYY-MM-DD) already matches, so it carries no flag.
        if (xui === "datetime") input.dataset.argFmt = "datetime";
      } else if (prop.type === "integer" || prop.type === "number") {
        input = document.createElement("input");
        input.type = "number";
        if (prop.type === "number") input.step = "any";
        if (prop.default !== undefined) input.value = String(prop.default);
        if (required.has(name)) input.required = true;
        if (prop.description) input.placeholder = prop.description;
      } else {
        // Plain string — auto-growing textarea, Shift+Enter = newline.
        input = document.createElement("textarea");
        input.rows = 1;
        if (prop.default !== undefined) input.value = String(prop.default);
        if (required.has(name)) input.required = true;
        if (prop.description) input.placeholder = prop.description;
        const grow = () => {
          input.style.height = "auto";
          input.style.height = input.scrollHeight + "px";
        };
        input.addEventListener("input", grow);
        input.addEventListener("keydown", (e) => {
          if (e.key === "Enter" && !e.shiftKey) e.preventDefault();
        });
        requestAnimationFrame(grow);
      }
      input.dataset.arg = name;
      input.dataset.argType =
        xui === "date" || xui === "datetime" ? "string" : prop.type || "string";
      label.append(input);
      if (prop.description && input.tagName === "SELECT") {
        const hint = document.createElement("span");
        hint.className = "cat-desc";
        hint.textContent = prop.description;
        label.append(hint);
      }
      return label;
    }

    // Optional/defaulted args fold into a collapsed "Default arguments" section
    // near the top; required args stay always-visible. Required == in the schema
    // `required` set (mirrors the React partition by !arg.required).
    const entries = Object.entries(props);
    const defaulted = entries.filter(([n]) => !required.has(n));
    const requiredEntries = entries.filter(([n]) => required.has(n));
    if (defaulted.length) {
      const det = document.createElement("details");
      det.className = "default-args";
      const sum = document.createElement("summary");
      sum.textContent = `Default arguments (${defaulted.length})`;
      det.append(sum);
      for (const [name, prop] of defaulted) det.append(buildField(name, prop));
      reqForm.append(det);
    }
    for (const [name, prop] of requiredEntries) reqForm.append(buildField(name, prop));

    const row = document.createElement("div");
    row.className = "form-row";
    const submit = document.createElement("button");
    submit.type = "submit";
    submit.textContent = "Request";
    const back = document.createElement("button");
    back.type = "button";
    back.className = "ghost";
    back.textContent = "Back";
    back.addEventListener("click", renderCatalog);
    row.append(submit, back);
    reqForm.append(row);

    reqForm.onsubmit = (e) => {
      e.preventDefault();
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const args = {};
      for (const el of reqForm.querySelectorAll("[data-arg]")) {
        const name = el.dataset.arg;
        const type = el.dataset.argType;
        if (el.type === "checkbox") {
          if (el.checked) args[name] = true; // flags: present means on
        } else if (el.value !== "") {
          if (type === "integer") {
            args[name] = parseInt(el.value, 10);
          } else if (type === "number") {
            args[name] = parseFloat(el.value);
          } else if (el.dataset.argFmt === "datetime") {
            // Local zoneless value → UTC ISO string with Z (desktop parity);
            // fall back to the raw value if the browser can't parse it.
            const d = new Date(el.value);
            args[name] = isNaN(d.getTime()) ? el.value : d.toISOString();
          } else {
            args[name] = el.value;
          }
        }
      }
      const kind = tool["x-kind"] === "skill" ? "skill" : "runnable";
      const hostEl = reqForm.querySelector("[data-host]");
      const frame = { type: "request", tool: tool.name, args, kind };
      if (hostEl && hostEl.value) frame.host = hostEl.value;
      ws.send(JSON.stringify(frame));
      renderCatalog(); // back to the list; the echo lands in the transcript
    };
  }

  // ── connection ─────────────────────────────────────────────────────────────

  function connect() {
    ws = new WebSocket(wsUrl());
    setStatus("connecting…", "warn");
    ws.onopen = () => {
      setStatus("connected", "ok");
      present.set(me, "available");
      renderPresence();
    };
    ws.onmessage = (ev) => {
      try {
        onFrame(JSON.parse(ev.data));
      } catch (_e) {
        /* ignore a malformed frame */
      }
    };
    ws.onclose = () => {
      setStatus("disconnected — retrying…", "warn");
      present.clear();
      renderPresence();
      setTimeout(connect, 1500); // simple auto-reconnect
    };
    ws.onerror = () => ws && ws.close();
  }

  // Enter the room. Called on Join (dev mode) and straight from whoami() once a
  // verified session is confirmed (no gate). A function declaration so whoami(),
  // which runs earlier, can call it.
  function doJoin() {
    me = ($("user").value || "guest").trim() || "guest";
    team = ($("team").value || "").trim();
    room = ($("room").value || "").trim();
    if (room) {
      titleEl.textContent = `#${room} · ${me}`;
    } else {
      // Helpdesk: join the single queue from /me. Show the team alongside the name.
      titleEl.textContent = `help desk · ${team ? `${me} · ${team}` : me}`;
    }
    join.hidden = true;
    app.hidden = false;
    if (!room) loadCatalog();
    connect();
  }

  join.addEventListener("submit", (e) => {
    e.preventDefault();
    doJoin();
  });

  $("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("text");
    const text = input.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "message", text }));
    input.value = "";
    input.focus();
  });
})();
