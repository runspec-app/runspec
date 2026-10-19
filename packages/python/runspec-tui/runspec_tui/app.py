"""app.py — the Textual app: runnables on the left, the run form on the right,
the run's output underneath. Mirrors runspec-console's Forms view."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from rich.json import JSON
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, Collapsible, Footer, Header, Input, Label, OptionList, RichLog, Static, Switch
from textual.widgets.option_list import Option

from . import catalog, form
from .catalog import Node, Runnable
from .fields import ArgField, autonomy_tag
from .runner import LABEL, Run

_JSON_RERENDER_LIMIT = 1_000_000
"""Output larger than this (bytes) stays as streamed instead of pretty-printed."""


def _effective_user() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:  # noqa: BLE001 — Windows
        return os.environ.get("USERNAME", "")


class RunspecTUI(App[None]):
    """The runspec Forms view in a terminal."""

    CSS_PATH = "app.tcss"
    TITLE = "runspec"
    BINDINGS = [
        Binding("ctrl+r", "run", "Run", priority=True),
        Binding("ctrl+t", "stop", "Stop", priority=True),
        Binding("slash", "focus_search", "Search"),
        Binding("escape", "back", "Back"),
        Binding("f2", "browse", "Browse", priority=True),
        Binding("ctrl+o", "toggle_output", "Output", priority=True),
        Binding("ctrl+y", "copy_output", "Copy output", priority=True),
    ]

    def __init__(
        self,
        runnables: list[Runnable] | None = None,
        *,
        include_hidden: bool = False,
        venv: str | None = None,
        user: str | None = None,
    ) -> None:
        super().__init__()
        self.runnables = catalog.discover(include_hidden) if runnables is None else runnables
        self.venv = venv if venv is not None else sys.prefix
        self.sub_title = f"{self.venv}  ·  {user if user is not None else _effective_user()}"
        self.current: Runnable | None = None
        self.path: tuple[str, ...] = ()
        self.env_supplied: set[str] = set()
        self.run_handle: Run | None = None
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self._out_bytes = 0
        self._header = ""

    # ── layout ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Input(placeholder="Search runnables   /", id="search")
                yield OptionList(id="runnables")
                yield Static(id="count")
            with Vertical(id="main"):
                yield VerticalScroll(id="pane")
                yield Static(id="help", markup=False)
                with Vertical(id="output"):
                    yield Static(id="output-title")
                    yield RichLog(id="log", wrap=True, markup=False, highlight=False)
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#output").display = False
        self._fill_list("")
        await self._show_welcome()
        self.watch(self.screen, "focused", self._focus_changed)
        self.query_one("#search").focus() if self.runnables else None

    # ── sidebar ─────────────────────────────────────────────────────────────

    def _fill_list(self, query: str) -> None:
        listing = self.query_one("#runnables", OptionList)
        listing.clear_options()
        hits = [r for r in self.runnables if catalog.matches(r, query)]
        for package, items in catalog.by_package(hits).items():
            listing.add_option(Option(Text.assemble((package, "bold"), (f"  {len(items)}", "dim")), disabled=True))
            for r in items:
                dot_style = {"autonomous": "green", "supervised": "yellow", "manual": "red"}.get(r.autonomy, "grey62")
                listing.add_option(Option(Text.assemble(("● ", dot_style), r.name), id=f"r:{self.runnables.index(r)}"))
        total = len(self.runnables)
        self.query_one("#count", Static).update(Text(f"{len(hits)} of {total} runnables" if query else f"{total} runnables", style="dim"))

    @on(Input.Changed, "#search")
    def _search(self, event: Input.Changed) -> None:
        self._fill_list(event.value)

    @on(Input.Submitted, "#search")
    async def _search_enter(self, event: Input.Submitted) -> None:
        hits = [r for r in self.runnables if catalog.matches(r, event.value)]
        if hits:
            await self.open(hits[0])

    @on(OptionList.OptionSelected, "#runnables")
    async def _picked(self, event: OptionList.OptionSelected) -> None:
        if event.option.id and event.option.id.startswith("r:"):
            await self.open(self.runnables[int(event.option.id[2:])])

    @on(OptionList.OptionSelected, "#commands")
    async def _command_picked(self, event: OptionList.OptionSelected) -> None:
        if self.current and event.option.id:
            await self.open(self.current, (*self.path, event.option.id))

    # ── the form pane ───────────────────────────────────────────────────────

    async def _show_welcome(self) -> None:
        pane = self.query_one("#pane", VerticalScroll)
        await pane.remove_children()
        if self.runnables:
            body = Text.assemble(
                ("runspec\n\n", "bold"),
                (f"{len(self.runnables)} runnables in {self.venv}\n\n", ""),
                ("Pick one on the left, or press / to search.\n\n", "dim"),
                ("Ctrl+R run · F2 browse for a path · Esc back · Ctrl+O output · Ctrl+Q quit", "dim"),
            )
        else:
            body = Text.assemble(
                ("No runnables here\n\n", "bold"),
                (f"Nothing in {self.venv} ships a runspec.toml with an installed entry point.\n", ""),
                ("Install a runnable package into this venv (pip install <package>) and start runspec-tui again.", "dim"),
            )
        await pane.mount(Static(body, id="welcome"))

    async def open(self, runnable: Runnable, path: tuple[str, ...] = ()) -> None:
        """Show the form for ``runnable`` at command ``path``."""
        self.current, self.path = runnable, path
        node = runnable.root.at(path)
        env_file = self._env_file(runnable)
        environ = {} if runnable.run_as else dict(os.environ)
        sources = {a["name"]: form.env_source(a, runnable.name, env_file, environ) for a in node.args}
        self.env_supplied = {name for name, src in sources.items() if src}

        widgets: list[Widget] = [Static(self._crumbs(runnable, node), id="crumbs")]
        if node.description:
            widgets.append(Static(node.description, id="description", markup=False))
        if node.commands:
            widgets.append(Label("COMMANDS", classes="section"))
            widgets.append(self._commands(node))
        if node.can_run:
            widgets += self._form(node, sources)
        else:
            widgets.append(Static(Text("Choose a command to run.", style="dim"), classes="note"))

        pane = self.query_one("#pane", VerticalScroll)
        await pane.remove_children()
        await pane.mount_all(widgets)
        self._sync_runbar()
        pane.scroll_home(animate=False)
        target = self._first_focus(node)
        if target is not None:
            target.focus(scroll_visible=False)

    def _env_file(self, runnable: Runnable) -> dict[str, str]:
        from runspec.env import load_env_file

        try:
            return load_env_file(runnable.raw, runnable.name)
        except OSError:  # e.g. a 0600 file owned by the venv owner — the run still reads it
            return {}

    def _crumbs(self, runnable: Runnable, node: Node) -> Text:
        segments = [runnable.name, *self.path]
        t = Text()
        for i, seg in enumerate(segments):
            if i:
                t.append(" › ", style="dim")
            t.append(seg, style="bold" if i == len(segments) - 1 else "dim")
        t.append("  ")
        t.append_text(autonomy_tag(node.autonomy))
        if runnable.run_as:
            t.append(f"   runs as {runnable.run_as}", style="cyan")
        t.append(f"   {runnable.package}", style="dim")
        return t

    def _commands(self, node: Node) -> OptionList:
        options = []
        for name, cmd in node.commands.items():
            own = [a for a in cmd.args if not a.get("inherited")]
            prompt = Text.assemble((name, "bold"), "  ", autonomy_tag(cmd.autonomy))
            if own:
                prompt.append(f"  {len(own)} args", style="dim")
            if cmd.commands:
                prompt.append(f"  {len(cmd.commands)} commands", style="dim")
            if cmd.description:
                prompt.append(f"\n{cmd.description}", style="dim")
            options.append(Option(prompt, id=name))
        return OptionList(*options, id="commands")

    def _form(self, node: Node, sources: dict[str, str | None]) -> list[Widget]:
        widgets: list[Widget] = []
        inherited, own = form.split_inherited(node.args)

        def group(args: list[dict[str, Any]]) -> list[Widget]:
            visible, defaulted = form.partition_args(args)
            out: list[Widget] = []
            if defaulted:
                out.append(Collapsible(*[self._field(a, sources) for a in defaulted], title=f"Default arguments ({len(defaulted)})", collapsed=True))
            out += [self._field(a, sources) for a in visible]
            return out

        if not node.args:
            widgets.append(Static(Text("No arguments — ready to run.", style="dim"), classes="note"))
        elif inherited:
            widgets.append(Label("INHERITED", classes="section"))
            widgets += group(inherited)
            if own:
                widgets.append(Label("THIS COMMAND", classes="section"))
                widgets += group(own)
        else:
            widgets += group(own)

        widgets.append(Static("", id="group-errors"))
        bar: list[Widget] = [Button("Run  ^R", variant="primary", id="run"), Button("Stop  ^T", variant="error", id="stop")]
        if form.offer_debug(node.args):
            bar += [Switch(id="debug"), Label("Debug logging", id="debug-label")]
        widgets.append(Horizontal(*bar, id="runbar"))
        return widgets

    def _field(self, arg: dict[str, Any], sources: dict[str, str | None]) -> ArgField:
        source = sources.get(arg["name"])
        kind = form.widget_kind(arg)
        value = form.initial_value(arg)
        if source and kind not in ("flag", "choice", "multichoice"):
            value = [] if kind == "lines" else ""  # blank, so the environment's value applies
        return ArgField(arg, value, source)

    def _first_focus(self, node: Node) -> Widget | None:
        if node.commands and not self.path:
            return self.query_one("#commands")
        for field in self.query(ArgField):
            if field.display and not any(isinstance(a, Collapsible) and a.collapsed for a in field.ancestors):
                return field.query_one("#control")
        buttons = self.query("#run")
        return buttons.first() if buttons else None

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Show Run/Stop in the footer only when they'd do something."""
        if action == "stop":
            return self.run_handle is not None
        if action == "run":
            return self.run_handle is None and self.current is not None and self.current.root.at(self.path).can_run
        if action == "copy_output":
            return bool(self._stdout)
        return True

    def _sync_runbar(self) -> None:
        self.refresh_bindings()
        running = self.run_handle is not None
        for button in self.query("#run"):
            button.disabled = running
        for button in self.query("#stop"):
            button.display = running

    # ── focus help ──────────────────────────────────────────────────────────

    def _focus_changed(self, widget: Widget | None) -> None:
        help_line = self.query_one("#help", Static)
        field = next((w for w in (widget.ancestors_with_self if widget else []) if isinstance(w, ArgField)), None)
        if field is not None and field.arg.get("description"):
            help_line.update(Text(field.arg["description"], style="dim"))
            help_line.display = True
        else:
            help_line.display = False

    # ── running ─────────────────────────────────────────────────────────────

    @on(Button.Pressed, "#run")
    def _run_pressed(self) -> None:
        self.action_run()

    @on(Button.Pressed, "#stop")
    def _stop_pressed(self) -> None:
        self.action_stop()

    def action_run(self) -> None:
        if self.current is None or self.run_handle is not None:
            return
        node = self.current.root.at(self.path)
        if not node.can_run:
            self.notify("Choose a command first.", severity="warning")
            return
        fields = {f.arg["name"]: f for f in self.query(ArgField)}
        values = {name: f.value for name, f in fields.items()}
        errors, group_errors = form.validate(node.args, node.groups, values, self.env_supplied)
        first: ArgField | None = None
        for name, field in fields.items():
            field.set_error(errors.get(name))
            if name in errors and first is None:
                first = field
        self.query_one("#group-errors", Static).update(Text("\n".join(group_errors), style="red"))
        if errors or group_errors:
            if first is not None:
                for ancestor in first.ancestors:
                    if isinstance(ancestor, Collapsible):
                        ancestor.collapsed = False
                first.focus_control()
                first.scroll_visible()
            count = len(errors) + len(group_errors)
            self.notify(f"Fix {count} problem{'s' if count > 1 else ''} before running.", severity="error")
            return
        debug_switch = self.query("#debug")
        debug = bool(debug_switch and debug_switch.first(Switch).value)
        argv, env = form.build_command(self.current.name, self.path, node.args, values, debug)
        self._start(self.current, argv, env)

    def _start(self, runnable: Runnable, argv: list[str], env: dict[str, str]) -> None:
        self._stdout, self._stderr, self._out_bytes = [], [], 0
        self._header = form.display_command(runnable.name, argv, env)
        output = self.query_one("#output")
        output.display = True
        log = self.query_one("#log", RichLog)
        log.clear()
        log.write(Text(f"$ {self._header}", style="bold"))
        where = f"as {runnable.run_as}" if runnable.run_as else "as you"
        self.query_one("#output-title", Static).update(Text.assemble(("Output ", "bold"), (f"· {runnable.name} · running {where}…", "dim")))
        self.run_handle = Run(
            runnable.name,
            argv,
            env,
            on_line=lambda stream, line: self.call_from_thread(self._line, stream, line),
            on_done=lambda code, secs: self.call_from_thread(self._done, runnable.name, code, secs),
        )
        self._sync_runbar()
        self.run_handle.start()

    def _line(self, stream: str, line: str) -> None:
        (self._stderr if stream == "stderr" else self._stdout).append(line)
        self._out_bytes += len(line) + 1
        self.query_one("#log", RichLog).write(Text(line, style="yellow" if stream == "stderr" else ""))

    def _done(self, name: str, code: int, seconds: float) -> None:
        handle, self.run_handle = self.run_handle, None
        stopped = bool(handle and handle.cancelled)
        log = self.query_one("#log", RichLog)
        text = "\n".join(self._stdout).strip()
        if self._stdout and self._out_bytes < _JSON_RERENDER_LIMIT and text[:1] in "{[":
            try:
                pretty = JSON.from_data(json.loads(text))
            except ValueError:
                pretty = None
            if pretty is not None:
                log.clear()
                log.write(Text(f"$ {self._header}", style="bold"))
                log.write(pretty)
                for line in self._stderr:
                    log.write(Text(line, style="yellow"))
        if stopped:
            status = Text(f"■ stopped · exit {code} · {seconds:.1f}s", style="bold yellow")
        elif code == 0:
            status = Text(f"✓ exit 0 · {seconds:.1f}s", style="bold green")
        else:
            status = Text(f"✗ exit {code} · {seconds:.1f}s", style="bold red")
        log.write(status)
        self.query_one("#output-title", Static).update(Text.assemble(("Output ", "bold"), (f"· {name} · recorded — runspec history list --label {LABEL}", "dim")))
        self._sync_runbar()
        if code == 0:
            self.notify(f"{name}: {status.plain}")
        else:
            self.notify(f"{name}: {status.plain}", severity="warning" if stopped else "error")

    def action_stop(self) -> None:
        if self.run_handle is not None:
            self.run_handle.cancel()
            self.notify("Stopping…")

    # ── other actions ───────────────────────────────────────────────────────

    def action_focus_search(self) -> None:
        self.query_one("#search").focus()

    async def action_back(self) -> None:
        focused = self.focused
        if isinstance(focused, Input) and focused.id == "search":
            if focused.value:
                focused.value = ""
            elif self.runnables:
                self.query_one("#runnables").focus()
            return
        if self.current is not None and self.path:
            await self.open(self.current, self.path[:-1])
            return
        self.query_one("#runnables").focus()

    def action_browse(self) -> None:
        focused = self.focused
        field = next((w for w in (focused.ancestors_with_self if focused else []) if isinstance(w, ArgField)), None)
        if field is not None and field.kind in ("path", "directory"):
            field.browse()
        else:
            self.notify("Focus a path field to browse.", severity="warning")

    def action_toggle_output(self) -> None:
        output = self.query_one("#output")
        output.display = not output.display

    def action_copy_output(self) -> None:
        if not self._stdout:
            self.notify("No output to copy yet.", severity="warning")
            return
        self.copy_to_clipboard("\n".join(self._stdout))
        self.notify(f"Copied {len(self._stdout)} lines of output to the clipboard.")

    async def action_quit(self) -> None:
        if self.run_handle is not None:
            self.run_handle.cancel(grace=1.0)
        self.exit()
