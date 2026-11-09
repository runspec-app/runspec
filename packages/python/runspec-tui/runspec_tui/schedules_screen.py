"""schedules_screen.py — the Schedules screen (F3): this venv's runspec-scheduler.

Lists every schedule the venv's scheduler knows — its own and the ones its
installed packages offer — with when it fires next, how its last run went and
whether someone is handling a failure, and acts on them through the
scheduler's own verbs (:mod:`runspec_tui.schedules`). It reads ``status`` every
few seconds on a worker thread, so a slow crontab read never freezes the
screen.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Select, Static

from . import schedules as sch

REFRESH_SECONDS = 10.0

CallFn = Callable[[sch.Command], sch.Result]


def now() -> _dt.datetime:
    """The local time now (a function so tests can pin it)."""
    return _dt.datetime.now().astimezone()


# ── small dialogs ─────────────────────────────────────────────────────────────


class ConfirmScreen(ModalScreen[bool]):
    """Yes / no."""

    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("y", "yes", "Yes", show=False)]

    def __init__(self, question: str, yes: str = "Yes") -> None:
        super().__init__()
        self.question, self.yes = question, yes

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.question, markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button(self.yes, variant="primary", id="yes")
                yield Button("Cancel", id="no")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class NoteScreen(ModalScreen["str | None"]):
    """One line of text (a note for "I'm on it" / "dealt with")."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, placeholder: str, *, required: bool) -> None:
        super().__init__()
        self.title_text, self.placeholder, self.required = title, placeholder, required

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.title_text, markup=False)
            yield Input(placeholder=self.placeholder, id="note")
            yield Static("", id="note-error")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#note", Input).focus()

    @on(Input.Submitted)
    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        text = " ".join(self.query_one("#note", Input).value.split())
        if self.required and not text:
            self.query_one("#note-error", Static).update(Text("Say what was done, so the next person knows.", style="red"))
            return
        self.dismiss(text)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class TimingScreen(ModalScreen["dict[str, Any] | None"]):
    """When a schedule fires: kind + value + zone, and its id for a new one.
    Dismisses with ``{"id", "timing"}`` (``timing`` = ``add``'s timing args)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, *, kind: str = "daily", value: str = "", zone: str = "", sid: str | None = None, taken: frozenset[str] = frozenset(), daemon: bool = True) -> None:
        super().__init__()
        self.title_text, self.kind, self.value, self.zone = title, kind, value, zone
        self.sid, self.taken, self.daemon = sid, taken, daemon

    def compose(self) -> ComposeResult:
        kinds = [(label, key) for key, label in sch.TIMING_KINDS if key != "every" or self.daemon]
        with Vertical(classes="dialog"):
            yield Static(self.title_text, markup=False)
            if self.sid is not None:
                yield Label("Id")
                yield Input(self.sid, id="sid")
            yield Label("When")
            yield Select(kinds, value=self.kind if any(k == self.kind for _, k in kinds) else "daily", allow_blank=False, id="kind")
            yield Input(self.value, placeholder="02:30", id="value")
            yield Label("Time zone (blank = this venv's)")
            yield Input(self.zone, placeholder="Europe/London", id="zone")
            yield Static("", id="timing-error")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self._placeholder()
        self.query_one("#value", Input).focus()

    @on(Select.Changed, "#kind")
    def _placeholder(self) -> None:
        kind = self.query_one("#kind", Select).value
        self.query_one("#value", Input).placeholder = {"cron": "0 2 * * *", "every": "15m"}.get(str(kind), "02:30")

    @on(Input.Submitted)
    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        error = self.query_one("#timing-error", Static)
        sid = self.sid
        if sid is not None:
            sid = self.query_one("#sid", Input).value.strip()
            problem = sch.id_problem(sid) or ("another schedule has this id" if sid in self.taken else "")
            if problem:
                error.update(Text(f"Id: {problem}", style="red"))
                return
        kind = str(self.query_one("#kind", Select).value)
        timing, problem = sch.timing_args(kind, self.query_one("#value", Input).value, self.query_one("#zone", Input).value)
        if problem:
            error.update(Text(problem, style="red"))
            return
        self.dismiss({"id": sid, "timing": timing})

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


POLICY_CHOICES = [
    ("Offer them (off until turned on)", "offer"),
    ("Turn them on automatically", "auto"),
    ("Ignore them", "off"),
]


class SettingsScreen(ModalScreen["dict[str, str] | None"]):
    """The venv's scheduler settings."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, settings: dict[str, Any], host_zone: str) -> None:
        super().__init__()
        self.settings, self.host_zone = settings, host_zone

    def compose(self) -> ComposeResult:
        s = self.settings

        def secs(key: str) -> str:
            value = s.get(key)
            return "" if value is None else str(value)

        with Vertical(classes="dialog"):
            yield Static(Text("Scheduler settings for this venv", style="bold"))
            yield Label(f"Time zone (blank = the host's, {self.host_zone or 'unknown'})")
            yield Input(str(s.get("timezone") or ""), placeholder="Europe/London", id="timezone")
            yield Label("Schedules offered by installed packages")
            yield Select(POLICY_CHOICES, value=str(s.get("package_schedules") or "offer"), allow_blank=False, id="package_schedules")
            if s.get("backend") == "daemon":
                yield Label("Run time limit (seconds, or 90m / 6h; 0 = none)")
                yield Input(secs("timeout"), id="timeout")
                yield Label("Fire a late run up to (misfire grace)")
                yield Input(secs("misfire_grace"), id="misfire_grace")
                yield Label("Catch up missed runs from the last")
                yield Input(secs("catch_up_window"), id="catch_up_window")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#timezone", Input).focus()

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        out: dict[str, str] = {}
        for widget in self.query(Input):
            out[str(widget.id)] = widget.value
        out["package_schedules"] = str(self.query_one("#package_schedules", Select).value)
        self.dismiss(out)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


# ── the screen ────────────────────────────────────────────────────────────────


class SchedulesScreen(Screen[None]):
    """This venv's schedules."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("f5", "refresh", "Refresh", show=False),
        Binding("c", "claim", "I'm on it"),
        Binding("x", "resolve", "Dealt with"),
        Binding("n", "run_now", "Run now"),
        Binding("o", "on_off", "On/off"),
        Binding("e", "edit", "Edit time"),
        Binding("a", "add", "Add"),
        Binding("d", "remove", "Remove"),
        Binding("u", "release", "Release", show=False),
        Binding("y", "sync", "Sync crontab"),
        Binding("r", "restart", "Restart"),
        Binding("s", "settings", "Settings"),
        Binding("b", "backend", "Backend", show=False),
    ]

    def __init__(self, caller: sch.Caller, call: CallFn | None = None, *, auto_refresh: bool = True) -> None:
        super().__init__()
        self.caller = caller
        self.call: CallFn = call or sch.call
        self.auto_refresh = auto_refresh
        self.payload: dict[str, Any] = {}
        self.rows: dict[str, dict[str, Any]] = {}
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="sched-body"):
            yield Static(Text("Reading the scheduler…", style="dim"), id="sched-strip")
            yield DataTable(id="sched-table", cursor_type="row", zebra_stripes=True)
            yield VerticalScroll(Static(id="sched-detail"), id="sched-detail-pane")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "Schedules"
        table = self.query_one("#sched-table", DataTable)
        for label, key in (("Id", "id"), ("State", "state"), ("Runnable", "runs"), ("When", "when"), ("Next", "next"), ("Last run", "last")):
            table.add_column(label, key=key)
        table.focus()
        self.action_refresh()
        if self.auto_refresh:
            self.set_interval(REFRESH_SECONDS, self.action_refresh)

    # ── reading ──────────────────────────────────────────────────────────────

    @work(thread=True, exclusive=True, group="status")
    def action_refresh(self) -> None:
        result = self.call(self.caller.command("status"))
        self.app.call_from_thread(self.show, result)

    def show(self, result: sch.Result) -> None:
        """Fill the strip, table and detail from a ``status`` result."""
        strip = self.query_one("#sched-strip", Static)
        if not result.payload.get("schedules") and not result.ok:
            strip.update(Text(f"Couldn't read the scheduler: {result.message}", style="red"))
            return
        self.payload = result.payload
        if hasattr(self.app, "last_schedules"):
            self.app.last_schedules = self.payload
        strip.update(self._strip())
        moment = now()
        backend = str(self.payload.get("backend") or "cron")
        table = self.query_one("#sched-table", DataTable)
        keep = table.cursor_row
        current = self._selected_id()
        table.clear()
        self.rows = {}
        records = sorted(self.payload.get("schedules") or [], key=lambda r: (bool(r.get("offered")), str(r.get("id"))))
        for rec in records:
            sid = str(rec.get("id"))
            self.rows[sid] = rec
            target = " ".join([str(rec.get("runnable") or ""), *[str(c) for c in rec.get("command") or []]])
            state, style = sch.state_text(rec, backend)
            table.add_row(sid, Text(state, style=style), target, sch.when_text(rec), sch.next_text(rec, moment), sch.last_text(rec, moment), key=sid)
        if current in self.rows:
            table.move_cursor(row=list(self.rows).index(current))
        elif self.rows:
            table.move_cursor(row=min(keep, len(self.rows) - 1))
        self._show_detail()
        self.refresh_bindings()

    def _strip(self) -> Text:
        p = self.payload
        daemon, style = sch.daemon_text(p)
        t = Text()
        t.append(f"{p.get('venv') or ''}", style="bold")
        t.append(f"   backend {p.get('backend') or 'cron'}")
        t.append("   daemon ")
        t.append(daemon, style=style)
        keepers = [k.get("manager") for k in p.get("keeper") or [] if isinstance(k, dict)]
        if keepers:
            t.append(f"   kept up by {', '.join(str(k) for k in keepers)}", style="dim")
        t.append(f"\nzone {sch.zone_text(p)}", style="dim")
        pkgs = p.get("packages") or {}
        policy = {"offer": "offered (off until turned on)", "auto": "turned on automatically", "off": "ignored"}.get(str(pkgs.get("policy") or "offer"), "")
        t.append(f"   package schedules {policy}", style="dim")
        if pkgs.get("offered"):
            t.append(f" · {pkgs['offered']} waiting to be turned on", style="cyan")
        if self.caller.as_owner:
            t.append(f"   changes run as {self.caller.owner}", style="dim")
        for problem in [*(p.get("errors") or []), *(pkgs.get("errors") or [])]:
            t.append(f"\n⚠ {problem}", style="red")
        for orphan in pkgs.get("orphans") or []:
            t.append(f"\n⚠ {orphan.get('id')}: {orphan.get('reason')}", style="yellow")
        for stale in p.get("stale") or []:
            t.append(f"\n⚠ crontab line for {stale.get('id')} ({stale.get('reason')}) — Sync crontab removes it", style="yellow")
        return t

    def _selected_id(self) -> str | None:
        tables = self.query("#sched-table").results(DataTable)
        table = next(iter(tables), None)
        if table is None or not self.rows or table.cursor_row < 0 or table.cursor_row >= table.row_count:
            return None
        return str(table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value)

    def selected(self) -> dict[str, Any] | None:
        sid = self._selected_id()
        return self.rows.get(sid) if sid else None

    @on(DataTable.RowHighlighted, "#sched-table")
    def _show_detail(self) -> None:
        detail = self.query_one("#sched-detail", Static)
        rec = self.selected()
        self.refresh_bindings()
        if rec is None:
            hint = "No schedules in this venv yet. Press a to add one."
            detail.update(Text(hint, style="dim"))
            return
        moment = now()
        body = Text()
        for i, (line, style) in enumerate(sch.detail_lines(rec, moment)):
            if i:
                body.append("\n")
            body.append(line, style=style)
        detail.update(body)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Show only the keys that would do something for the selected row."""
        rec = self.selected()
        backend = self.payload.get("backend")
        state = ((rec or {}).get("attention") or {}).get("state")
        if action in ("run_now", "on_off", "edit", "remove"):
            return rec is not None
        if action in ("claim", "resolve"):
            return state in ("failed", "handling")
        if action == "release":
            return state in ("handling", "done")
        if action == "sync":
            return backend == "cron"
        if action == "restart":
            return backend == "daemon"
        return True

    # ── doing ────────────────────────────────────────────────────────────────

    @work(thread=True, exclusive=True, group="act")
    def _do(self, cmd: sch.Command, done: str, sync: bool = False) -> None:
        """Run ``cmd``; with ``sync`` on a cron venv, bring the crontab in line
        afterwards (a config change does nothing on cron until then)."""
        result = self.call(cmd)
        if result.ok and sync and self.payload.get("backend", "cron") == "cron":
            synced = self.call(self.caller.command("sync"))
            if not synced.ok:
                result, done = synced, ""
                result.payload.setdefault("error", f"saved, but the crontab wasn't updated: {synced.message}")
        self.app.call_from_thread(self._after, result, done)

    def _after(self, result: sch.Result, done: str) -> None:
        if result.ok:
            self.notify(done)
        else:
            self.notify(result.message, severity="error", timeout=10)
        self.action_refresh()

    def action_run_now(self) -> None:
        rec = self.selected()
        if rec is None:
            return
        sid = str(rec["id"])

        def go(yes: bool | None) -> None:
            if yes:
                self._do(self.caller.command("run-now", "--id", sid, "--detach"), f"Started {sid}. It shows here when it finishes.")

        self.app.push_screen(ConfirmScreen(f"Run {sid} now, the way the schedule would?", "Run now"), go)

    def action_on_off(self) -> None:
        rec = self.selected()
        if rec is None:
            return
        sid = str(rec["id"])
        if rec.get("enabled", True):
            self._do(self.caller.command("disable", "--id", sid), f"Paused {sid}.", sync=True)
        else:
            self._do(self.caller.command("enable", "--id", sid), f"Turned on {sid}.", sync=True)

    def action_edit(self) -> None:
        rec = self.selected()
        if rec is None:
            return
        problem = sch.edit_problem(rec)
        if problem:
            self.notify(f"Can't change {rec['id']} here: {problem}.", severity="warning", timeout=10)
            return
        kind, value, zone = sch.timing_prefill(rec)
        backend = str(self.payload.get("backend") or "cron")

        def save(result: dict[str, Any] | None) -> None:
            if not result:
                return
            default = (self.payload.get("settings") or {}).get("timeout")
            args, env = sch.add_argv(rec, result["timing"], backend=backend, default_timeout=default)
            self._do(self.caller.command("add", *args, env=env), f"Saved the new time for {rec['id']}.", sync=True)

        self.app.push_screen(TimingScreen(f"When should {rec['id']} run?", kind=kind, value=value, zone=zone, daemon=backend == "daemon"), save)

    def action_add(self) -> None:
        start = getattr(self.app, "start_schedule_mode", None)
        if start is not None:
            start()

    def action_remove(self) -> None:
        rec = self.selected()
        if rec is None:
            return
        sid = str(rec["id"])
        if rec.get("origin") == "package":
            if rec.get("offered") and not (rec.get("package") or {}).get("overridden"):
                self.notify(
                    f"{sid} comes from the {rec.get('runnable')} package and is just offered: there's nothing to remove. Press o to turn it on, or s to stop this venv listing package schedules.",
                    timeout=10,
                )
                return
            question = f"Put {sid} back to what the {rec.get('runnable')} package says, dropping this venv's changes?"
            yes, done = "Put back", f"{sid} is back to the package's version."
        else:
            question, yes, done = f"Remove the schedule {sid}? Its run history stays.", "Remove", f"Removed {sid}."

        def go(ok: bool | None) -> None:
            if ok:
                self._do(self.caller.command("remove", "--id", sid), done, sync=True)

        self.app.push_screen(ConfirmScreen(question, yes), go)

    def _note(self, verb: str, title: str, placeholder: str, *, required: bool, done: str) -> None:
        rec = self.selected()
        if rec is None:
            return
        label = f"sched:{rec['id']}"

        def save(note: str | None) -> None:
            if note is None:
                return
            args = ["--label", label] + (["--note", note] if note else [])
            self._do(self.caller.history(verb, *args), done.format(id=rec["id"]))

        self.app.push_screen(NoteScreen(title.format(id=rec["id"]), placeholder, required=required), save)

    def action_claim(self) -> None:
        self._note("claim", "You're looking at {id}'s failure. Anything to add? (optional)", "e.g. checking the disk on the host", required=False, done="Marked {id} as being handled by you.")

    def action_resolve(self) -> None:
        self._note("resolve", "What was done about {id}'s failure?", "e.g. freed space, re-ran it, fine now", required=True, done="Marked {id} as dealt with.")

    def action_release(self) -> None:
        rec = self.selected()
        if rec is None:
            return
        self._do(self.caller.history("release", "--label", f"sched:{rec['id']}"), f"Released {rec['id']}.")

    def action_sync(self) -> None:
        self._do(self.caller.command("sync"), "Crontab brought in line with the schedules.")

    def action_restart(self) -> None:
        def go(ok: bool | None) -> None:
            if ok:
                self._do(self.caller.command("restart"), "Restarted the scheduler daemon.")

        self.app.push_screen(ConfirmScreen("Restart the scheduler daemon? Runs in progress carry on."), go)

    def action_settings(self) -> None:
        before = dict(self.payload.get("settings") or {})

        def save(after: dict[str, str] | None) -> None:
            if after is None:
                return
            args = sch.settings_argv(before, after)
            if not args:
                self.notify("Nothing changed.")
                return
            self._do(self.caller.command("settings", *args), "Saved the scheduler settings.")

        self.app.push_screen(SettingsScreen(before, str(self.payload.get("host_zone") or "")), save)

    def action_backend(self) -> None:
        current = str(self.payload.get("backend") or "cron")
        other = "daemon" if current == "cron" else "cron"
        what = {
            "daemon": "Move this venv's schedules from crontab to the scheduler daemon? Its crontab lines are removed; the daemon must be running (and kept up) to fire them.",
            "cron": "Move this venv's schedules from the daemon back to crontab? Schedules that need the daemon (intervals, other zones) stop firing.",
        }[other]

        def go(ok: bool | None) -> None:
            if ok:
                self._do(self.caller.command("use", other), f"This venv now uses {other}.")

        self.app.push_screen(ConfirmScreen(what, f"Use {other}"), go)

    def action_back(self) -> None:
        self.app.pop_screen()
