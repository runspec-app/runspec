// app.js — the runspec room browser client (vanilla, no build step).
//
// Two modes share one client. Group chat: /ws?room=&user= — the original room.
// Runnables portal: /ws?queue=&user= — the server derives a private thread from
// the queue + the signed-in identity, and the aside shows the queue's live
// catalogue (the runnables the on-shift operator consoles publish). Picking one
// renders a form from its inputSchema; submitting sends a "request" frame the hub
// auto-assigns to an operator. Each request then shows its claim-free lifecycle
// (assigned → in progress → done/failed) inline; when no operator is on shift it
// shows "no operator available" with a Page-the-team button. See
// docs/design/runnables-portal.md §3.

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
  const reqForm = $("reqform");

  let ws = null;
  let me = "";
  let room = ""; // group-chat room (mode 1)
  let queue = ""; // helpdesk queue (mode 2; wins when set)
  let authed = false; // server has OIDC enabled and we're logged in
  let catalog = []; // the queue's published tools
  const present = new Map(); // user -> status
  const requests = new Map(); // request_id -> { status node } for live lifecycle

  // Ask the server who we are. In auth mode the name comes from the session and
  // the field is locked; in dev mode the user types a name.
  async function whoami() {
    try {
      const info = await (await fetch("/me")).json();
      authed = !!info.auth;
      if (info.user) {
        $("user").value = info.user;
        if (authed) $("user").readOnly = true;
      }
      if (authed) {
        $("user-label").hidden = true;
        $("join-hint").textContent = "Signed in via single sign-on.";
      }
    } catch (_e) {
      /* dev fallback: leave the name field editable */
    }
  }
  whoami();

  // Adapt the join form to the link. The product is the help desk, so the Room
  // (group-chat) field is hidden by default. A ?queue= visitor is the normal
  // path; a ?room= deep-link still works (group chat is hidden, not removed) and
  // re-reveals the Room field for that one visit.
  const params = new URLSearchParams(location.search);
  const urlQueue = (params.get("queue") || "").trim();
  const urlRoom = (params.get("room") || "").trim();
  // Dev-mode identity from the link (?user=alice). In auth mode whoami() fills
  // the name from the OIDC session and locks the field, overriding this.
  const urlUser = (params.get("user") || "").trim();
  if (urlUser) $("user").value = urlUser;
  if (urlQueue) {
    $("queue").value = urlQueue;
  } else if (urlRoom) {
    $("room").value = urlRoom;
    $("room-label").hidden = false; // legacy group-chat deep-link
    $("queue-label").hidden = true;
  } else {
    $("mode-hint").hidden = false;
  }

  function wsUrl() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const q = queue
      ? new URLSearchParams({ queue, user: me })
      : new URLSearchParams({ room, user: me });
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

  function renderCatalog() {
    catalogPanel.hidden = false;
    reqForm.hidden = true;
    catalogList.hidden = false;
    catalogList.innerHTML = "";
    if (!catalog.length) {
      const li = document.createElement("li");
      li.className = "cat-empty";
      li.textContent = "No runnables available — no operator may be on shift.";
      catalogList.append(li);
      return;
    }
    for (const tool of catalog) {
      const li = document.createElement("li");
      li.className = "cat-item";
      const name = document.createElement("span");
      name.className = "cat-name";
      name.textContent = tool.name;
      const desc = document.createElement("span");
      desc.className = "cat-desc";
      desc.textContent = tool.description || "";
      li.append(name, desc);
      li.addEventListener("click", () => renderForm(tool));
      catalogList.append(li);
    }
  }

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
    h.textContent = tool.name;
    reqForm.append(h);
    if (tool.description) {
      const p = document.createElement("p");
      p.className = "cat-desc";
      p.textContent = tool.description;
      reqForm.append(p);
    }

    const schema = tool.inputSchema || {};
    const props = schema.properties || {};
    const required = new Set(schema.required || []);

    for (const [name, prop] of Object.entries(props)) {
      const label = document.createElement("label");
      const caption = document.createElement("span");
      caption.textContent = name + (required.has(name) ? " *" : "");
      label.append(caption);

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
      } else {
        input = document.createElement("input");
        input.type =
          prop.type === "integer" || prop.type === "number" ? "number" : "text";
        if (prop.type === "number") input.step = "any";
        if (prop.default !== undefined) input.value = String(prop.default);
        if (required.has(name)) input.required = true;
        if (prop.description) input.placeholder = prop.description;
      }
      input.dataset.arg = name;
      input.dataset.argType = prop.type || "string";
      label.append(input);
      if (prop.description && input.tagName !== "INPUT") {
        const hint = document.createElement("span");
        hint.className = "cat-desc";
        hint.textContent = prop.description;
        label.append(hint);
      }
      reqForm.append(label);
    }

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
          args[name] =
            type === "integer"
              ? parseInt(el.value, 10)
              : type === "number"
                ? parseFloat(el.value)
                : el.value;
        }
      }
      ws.send(JSON.stringify({ type: "request", tool: tool.name, args }));
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

  join.addEventListener("submit", (e) => {
    e.preventDefault();
    me = ($("user").value || "guest").trim() || "guest";
    queue = ($("queue").value || "").trim();
    room = ($("room").value || "ops").trim() || "ops";
    titleEl.textContent = queue ? `help desk · ${queue} · ${me}` : `#${room} · ${me}`;
    join.hidden = true;
    app.hidden = false;
    if (authed) $("logout").hidden = false;
    if (queue) loadCatalog();
    connect();
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
