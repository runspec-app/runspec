# Local trace analysis with Phoenix

[Arize Phoenix](https://github.com/Arize-ai/phoenix) is an OTLP collector + an
LLM-aware trace-analysis web UI. It's a great way to inspect the console's agent
traces (`turn → LLM call → tool call`) **locally** — nothing leaves your machine.

runspec-console does **not** bundle or manage Phoenix. You run Phoenix yourself
(in its own venv) and point the console's [telemetry export](console-telemetry.md)
at it — Phoenix is just another OTLP backend, one that happens to run on
`localhost`. Keeping it separate means you **update Phoenix on its own schedule**
(independent of the console) and the console's venv stays lean.

> Earlier versions shipped a console-managed Phoenix (a Start/Stop control that
> launched `arize-phoenix` from the console's own venv). That was removed to
> decouple the two — see the CHANGELOG migration note.

## 1. Install Phoenix in its own venv

Keep `arize-phoenix` (a heavy dependency) out of the console's environment:

```bash
python -m venv ~/phoenix-venv
~/phoenix-venv/bin/pip install arize-phoenix      # Windows: ~\phoenix-venv\Scripts\pip
```

(Or use `pipx install arize-phoenix`, Docker `arizephoenix/phoenix`, etc. — however
you like to run it.)

## 2. Run it

```bash
~/phoenix-venv/bin/phoenix serve                  # serves the UI + OTLP on :6006
```

Leave it running. Its data lives in Phoenix's own working dir (default `~/.phoenix`).

## 3. Point the console at it

In runspec-console, open **Settings → Telemetry** and click **Use local Phoenix**.
That fills in:

- **Enable tracing** on,
- **OTLP endpoint** = `http://127.0.0.1:6006/v1/traces`,
- **Capture prompt / completion / argument text** on (safe here — it's local, and
  secret-looking arguments are still scrubbed).

Review and click **Save**. This uses the console's generic OTLP export path, which
needs only the `otel` extra:

```bash
pip install "runspec-console[otel]"
```

(You can of course set those fields by hand instead of using the preset — the same
`[telemetry]` config the [Telemetry docs](console-telemetry.md) describe.)

## 4. View the traces

Run an agent turn (a chat message, a trigger, a schedule), then open
`http://localhost:6006` in your browser and click into the **Default** project. New
turns appear within a few seconds (spans are batched).

## Troubleshooting

- **No traces appear** — confirm Settings → Telemetry shows **Enable tracing** on
  with the endpoint set, that `phoenix serve` is actually running on 6006, then run
  a fresh agent turn and click into the **Default** project in Phoenix.
- **Phoenix won't start / its log shows a database or migration error** — Phoenix's
  SQLite store (in its working dir, default `~/.phoenix`) can get into a bad state.
  Stop Phoenix and **delete that folder**; it recreates a fresh store on the next
  start. (You lose previously stored traces, not your console config.)
- **A rendering error in Phoenix's own UI** (e.g. a "Something went wrong" /
  minified React error in a span panel) — that's a Phoenix bug; **update Phoenix**
  in its venv (`pip install -U arize-phoenix`). Because it's decoupled from the
  console, you can do this any time.
