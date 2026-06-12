// app.js — the runspec room browser client (vanilla, no build step).
//
// Two modes share one client. Group chat: /ws?room=&user= — the original room.
// Help desk: /ws?queue=&user= — the server derives a private conversation room
// from the queue + the signed-in identity, and the aside shows the queue's
// self-service catalog (published by the on-shift agent consoles). Picking a
// catalog item renders a form generated from its inputSchema; submitting sends a
// structured "request" frame that lands in the thread for an agent to enact.

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const join = $("join");
  const app = $("app");
  const transcript = $("transcript");
  const presenceList = $("presence");
  const statusEl = $("status");
  const titleEl = $("title");
  const convEl = $("convstate");
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

  // Deep links: /?queue=it joins the help desk directly; /?room=ops a group room.
  const params = new URLSearchParams(location.search);
  if (params.get("queue")) $("queue").value = params.get("queue");
  if (params.get("room")) $("room").value = params.get("room");

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
    transcript.append(li);
    transcript.scrollTop = transcript.scrollHeight;
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

  // The thread's lifecycle, as a header chip: open / assigned to X / resolved /
  // closed. Driven entirely by the server's conversation frames.
  function renderConversation(frame) {
    convEl.hidden = false;
    convEl.className = "convstate " + frame.state;
    convEl.textContent =
      frame.state === "assigned" && frame.assignee
        ? `assigned · ${frame.assignee}`
        : frame.state;
  }

  function onFrame(frame) {
    if (frame.type === "message") {
      addMessage(frame);
    } else if (frame.type === "presence") {
      if (frame.status === "away") present.delete(frame.user);
      else present.set(frame.user, frame.status);
      renderPresence();
    } else if (frame.type === "conversation") {
      renderConversation(frame);
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
      li.textContent = "No services published — an agent may not be on shift.";
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
    catalogList.hidden = true;
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
