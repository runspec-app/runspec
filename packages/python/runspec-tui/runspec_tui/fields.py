"""fields.py — one form field per arg: its label, control and error line."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import Button, DirectoryTree, Input, Label, Select, SelectionList, Static, Switch, TextArea

from . import form

AUTONOMY = {
    "autonomous": ("auto", "green"),
    "confirm": ("confirm", "grey62"),
    "supervised": ("supervised", "yellow"),
    "manual": ("manual", "red"),
}


def autonomy_tag(level: str | None) -> Text:
    """The console's autonomy pill, as coloured text."""
    label, colour = AUTONOMY.get((level or "confirm").lower(), AUTONOMY["confirm"])
    return Text(f" {label} ", style=f"bold {colour} reverse")


class PathSuggester(Suggester):
    """Completes the next path segment from the local filesystem."""

    def __init__(self, dirs_only: bool = False) -> None:
        super().__init__(use_cache=False, case_sensitive=True)
        self.dirs_only = dirs_only

    async def get_suggestion(self, value: str) -> str | None:
        if not value:
            return None
        typed = Path(value).expanduser()
        base, stem = (typed, "") if value.endswith(("/", "\\")) else (typed.parent, typed.name)
        try:
            children = sorted(base.iterdir(), key=lambda p: p.name)
        except OSError:
            return None
        for child in children:
            if not child.name.startswith(stem) or child.name == stem:
                continue
            if self.dirs_only and not child.is_dir():
                continue
            prefix = value[: len(value) - len(stem)] if stem else value
            return prefix + child.name + ("/" if child.is_dir() else "")
        return None


class _DirsOnlyTree(DirectoryTree):
    def filter_paths(self, paths: Any) -> Any:
        return [p for p in paths if p.is_dir()]


class BrowseScreen(ModalScreen[str | None]):
    """Pick a file (or, for a folder arg, a folder) from a tree."""

    BINDINGS = [("escape", "dismiss_none", "Cancel")]

    def __init__(self, start: str, dirs_only: bool) -> None:
        super().__init__()
        self.dirs_only = dirs_only
        here = Path(start).expanduser() if start else Path.cwd()
        while not here.is_dir() and here != here.parent:
            here = here.parent
        self.root = here if here.is_dir() else Path.cwd()
        self.chosen: Path | None = self.root if dirs_only else None

    def compose(self) -> ComposeResult:
        with Vertical(id="browse-dialog"):
            yield Label(f"[b]{'Choose a folder' if self.dirs_only else 'Choose a file'}[/]  [dim]{self.root}[/]")
            tree_cls = _DirsOnlyTree if self.dirs_only else DirectoryTree
            yield tree_cls(self.root, id="browse-tree")
            with Horizontal(id="browse-buttons"):
                yield Button("Use this folder" if self.dirs_only else "Use this file", variant="primary", id="browse-ok")
                yield Button("Up a level", id="browse-up")
                yield Button("Cancel", id="browse-cancel")

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        if not self.dirs_only:
            self.dismiss(str(event.path))

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self.chosen = event.path

    def on_tree_node_highlighted(self, event: Any) -> None:
        data = getattr(event.node, "data", None)
        path = getattr(data, "path", None)
        if path is not None and (not self.dirs_only or Path(path).is_dir()):
            self.chosen = Path(path)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "browse-ok":
            if self.chosen is not None and (self.dirs_only or self.chosen.is_file()):
                self.dismiss(str(self.chosen))
            else:
                self.notify("Pick a file first", severity="warning")
        elif event.button.id == "browse-up":
            tree = self.query_one("#browse-tree", DirectoryTree)
            parent = Path(tree.path).parent
            tree.path = parent
            self.chosen = parent if self.dirs_only else None
        else:
            self.dismiss(None)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class ArgField(Vertical):
    """One arg: ``--name  type  tags`` / one-line description / control / error."""

    DEFAULT_CLASSES = "argfield"

    def __init__(self, arg: dict[str, Any], value: Any, source: str | None = None) -> None:
        super().__init__(id=f"field-{arg['name']}")
        self.arg = arg
        self.kind = form.widget_kind(arg)
        self.initial = value
        self.source = source

    # ── composition ──

    def _heading(self) -> Text:
        arg = self.arg
        t = Text()
        t.append(f"--{arg['name']}", style="bold")
        t.append("  ")
        t.append(f" {form.type_label(arg)} ", style="reverse dim")
        if arg.get("inherited"):
            t.append("  ")
            t.append(" inherited ", style="reverse blue")
        t.append("  ")
        if self.kind == "password":
            t.append("required secret" if arg.get("required") else "secret", style="dark_orange" if arg.get("required") else "dim")
        elif arg.get("required") and not self.source:
            t.append("required", style="dark_orange")
        elif self.source:
            t.append(f"from {self.source}", style="green")
        elif form.is_var_ref(arg.get("default")):
            t.append(str(arg["default"]), style="dark_orange")
        else:
            t.append("optional", style="dim")
        return t

    def compose(self) -> ComposeResult:
        yield Static(self._heading(), classes="arg-heading")
        if self.arg.get("description"):
            yield Static(self.arg["description"], classes="arg-desc", markup=False)
        yield from self._control()
        yield Static("", classes="arg-error")

    def _control(self) -> ComposeResult:
        arg, kind, value = self.arg, self.kind, self.initial
        cid = "control"
        hint = form.placeholder(arg)
        if self.source and kind not in ("flag", "choice", "multichoice"):
            hint = f"from {self.source} — type to override"
        if kind == "flag":
            yield Switch(value=bool(value), id=cid)
        elif kind == "choice":
            yield Select([(o, o) for o in arg["options"]], value=value if value in arg["options"] else Select.NULL, prompt="Select…", id=cid)
        elif kind == "multichoice":
            chosen = set(value or [])
            yield SelectionList(*[(o, o, o in chosen) for o in arg["options"]], id=cid)
        elif kind == "lines":
            yield TextArea("\n".join(value or []), id=cid, placeholder=hint, soft_wrap=True, tab_behavior="focus")
        elif kind in ("path", "directory"):
            with Horizontal(classes="path-row"):
                yield Input(value or "", placeholder=hint, suggester=PathSuggester(dirs_only=kind == "directory"), id=cid)
                yield Button("Browse", id="browse", classes="browse", compact=True)
        elif kind == "color":
            with Horizontal(classes="path-row"):
                yield Input(value or "", placeholder=hint, max_length=7, restrict=r"#?[0-9A-Fa-f]*", id=cid)
                yield Static("    ", id="swatch", classes="swatch")
        elif kind == "number":
            yield Input(value or "", placeholder=hint, type="integer" if arg.get("type") == "int" else "number", id=cid)
        elif kind == "password":
            yield Input(value or "", placeholder=hint, password=True, id=cid)
        else:
            restrict = {"date": r"[0-9-]*", "month": r"[0-9-]*", "time": r"[0-9:]*"}.get(kind)
            yield Input(value or "", placeholder=hint, restrict=restrict, id=cid)

    def on_mount(self) -> None:
        if self.kind == "color":
            self._paint_swatch(str(self.initial or ""))

    # ── value / error ──

    @property
    def value(self) -> Any:
        control = self.query_one("#control")
        if isinstance(control, Switch):
            return control.value
        if isinstance(control, Select):
            return None if control.value is Select.NULL else control.value
        if isinstance(control, SelectionList):
            return list(control.selected)
        if isinstance(control, TextArea):
            return control.text
        assert isinstance(control, Input)
        return control.value

    def set_value(self, value: str) -> None:
        control = self.query_one("#control")
        if isinstance(control, Input):
            control.value = value

    def set_error(self, message: str | None) -> None:
        self.set_class(bool(message), "-invalid")
        self.query_one(".arg-error", Static).update(Text(message or "", style="red"))

    def focus_control(self) -> None:
        self.query_one("#control").focus()

    # ── events ──

    def _changed(self) -> None:
        if self.has_class("-invalid"):
            self.set_error(None)

    def on_input_changed(self, event: Input.Changed) -> None:
        self._changed()
        if self.kind == "color":
            self._paint_swatch(event.value)

    def on_switch_changed(self, event: Switch.Changed) -> None:
        self._changed()

    def on_select_changed(self, event: Select.Changed) -> None:
        self._changed()

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged[Any]) -> None:
        self._changed()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._changed()

    def _paint_swatch(self, value: str) -> None:
        swatch = self.query_one("#swatch", Static)
        hexval = "#" + value.lstrip("#")
        if len(hexval) == 7:
            try:
                swatch.styles.background = hexval
                return
            except Exception:  # noqa: BLE001 — not a colour yet
                pass
        swatch.styles.background = None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "browse":
            event.stop()
            self.browse()

    def browse(self) -> None:
        if self.kind not in ("path", "directory"):
            return

        def _picked(path: str | None) -> None:
            if path:
                self.set_value(path)
                self.focus_control()

        self.app.push_screen(BrowseScreen(str(self.value or ""), self.kind == "directory"), _picked)
