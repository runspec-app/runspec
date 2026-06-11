// app.js — the runspec room browser client (vanilla, no build step).
//
// Opens a WebSocket to /ws?room=&user=, renders the same message/presence frames
// the consoles speak, and sends message frames from the composer. Backscroll and
// live messages arrive as the same "message" frame — we just append in order.

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const join = $("join");
  const app = $("app");
  const transcript = $("transcript");
  const presenceList = $("presence");
  const statusEl = $("status");
  const titleEl = $("title");

  let ws = null;
  let me = "";
  let room = "";
  let authed = false; // server has OIDC enabled and we're logged in
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

  function wsUrl(room, user) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const q = new URLSearchParams({ room, user });
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
    li.className = "msg" + (frame.sender === me ? " mine" : "");
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

  function onFrame(frame) {
    if (frame.type === "message") {
      addMessage(frame);
    } else if (frame.type === "presence") {
      if (frame.status === "away") present.delete(frame.user);
      else present.set(frame.user, frame.status);
      renderPresence();
    }
  }

  function connect() {
    ws = new WebSocket(wsUrl(room, me));
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
    room = ($("room").value || "ops").trim() || "ops";
    titleEl.textContent = `#${room} · ${me}`;
    join.hidden = true;
    app.hidden = false;
    if (authed) $("logout").hidden = false;
    connect();
  });

  $("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("text");
    const text = input.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "message", room, text }));
    input.value = "";
    input.focus();
  });
})();
