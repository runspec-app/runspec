"""mail_categories.py — Outlook categories that show a trigger's progress on the
email itself.

When ``[mail_categories] enabled = true``, the console tags each email a trigger
handles, so anyone looking at the mailbox in Outlook (classic, new or OWA) can see
what happened without opening the console:

- ``runspec: <trigger>`` — which trigger matched the email (added, never removed).
- one **status** at a time — ``runspec: awaiting claim``, ``runspec: done``,
  ``runspec: failed``, ``runspec: needs a person`` or ``runspec: skipped``. A new
  status replaces the old one. ``skipped`` (the rota or a locked screen held this
  console back) is *weak*: it is only set when no other status is there, so an
  off-shift console never hides another console's ``done`` or ``awaiting claim``.
- ``runspec: reply drafted`` — a reply was saved to Drafts (added).
- ``Claimed: <name>`` — written by the claim itself (``sources/outlook.py``
  ``claim_message``); claiming also clears the status.

The category names live on the email in Exchange, so they reach everyone with
access to the mailbox. Colours come from a master category list; with
``colors = true`` the console adds its categories to the operator's own list and
(best-effort) to the list of the mailbox the email is in.

This module is pure (no COM): settings, names, the per-stage change and the
merge. The COM write is ``sources.outlook.update_categories``; the bridge wiring
is ``bridge_mail_categories``. See docs/console-automation.md → "Outlook
categories".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_PREFIX = "runspec"

# Trigger lifecycle stages the engine/bridge report.
MATCHED = "matched"
AWAITING_CLAIM = "awaiting_claim"
DONE = "done"
FAILED = "failed"
NEEDS_PERSON = "needs_person"
SKIPPED = "skipped"
REPLY_DRAFTED = "reply_drafted"

STAGES = (MATCHED, AWAITING_CLAIM, DONE, FAILED, NEEDS_PERSON, SKIPPED, REPLY_DRAFTED)

# The visible label after the prefix, per stage that writes a fixed category.
_LABELS = {
    AWAITING_CLAIM: "awaiting claim",
    DONE: "done",
    FAILED: "failed",
    NEEDS_PERSON: "needs a person",
    SKIPPED: "skipped",
    REPLY_DRAFTED: "reply drafted",
}

# Stages whose category is the single status (mutually exclusive).
_STATUS_STAGES = (AWAITING_CLAIM, DONE, FAILED, NEEDS_PERSON, SKIPPED)

# Outlook OlCategoryColor values.
COLOR_RED = 1
COLOR_ORANGE = 2
COLOR_YELLOW = 4
COLOR_GREEN = 5
COLOR_TEAL = 6
COLOR_BLUE = 8
COLOR_PURPLE = 9
COLOR_GRAY = 13

_STAGE_COLORS = {
    AWAITING_CLAIM: COLOR_YELLOW,
    DONE: COLOR_GREEN,
    FAILED: COLOR_RED,
    NEEDS_PERSON: COLOR_ORANGE,
    SKIPPED: COLOR_GRAY,
    REPLY_DRAFTED: COLOR_TEAL,
}
TRIGGER_COLOR = COLOR_BLUE
CLAIMED_COLOR = COLOR_PURPLE


def _clean(text: Any) -> str:
    """A category-safe fragment: Outlook separates categories with ``,`` (and
    accepts ``;`` on input), so neither may appear inside a name."""
    out = str(text or "").replace(",", " ").replace(";", " ")
    return " ".join(out.split())


def read_settings(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Normalise a ``[mail_categories]`` table: ``enabled`` (default false),
    ``prefix`` (default ``"runspec"``), ``colors`` (default true)."""
    raw = cfg if isinstance(cfg, dict) else {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "prefix": _clean(raw.get("prefix")) or DEFAULT_PREFIX,
        "colors": bool(raw.get("colors", True)),
    }


def category(prefix: str, label: str) -> str:
    """``"<prefix>: <label>"``, both made category-safe."""
    return f"{_clean(prefix) or DEFAULT_PREFIX}: {_clean(label)}"


def trigger_category(prefix: str, trigger_name: str) -> str:
    """The category naming the trigger that matched (``runspec: disk-alerts``)."""
    return category(prefix, trigger_name or "trigger")


def status_categories(prefix: str) -> list[str]:
    """Every status category for ``prefix`` — what a new status (or a claim)
    replaces."""
    return [category(prefix, _LABELS[s]) for s in _STATUS_STAGES]


@dataclass(frozen=True)
class Change:
    """What one stage does to an email's categories."""

    add: tuple[str, ...] = ()
    status: str = ""
    weak: bool = False
    remove: tuple[str, ...] = ()
    # name → OlCategoryColor, for the master category lists.
    colors: dict[str, int] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.add or self.status or self.remove)


def stage_change(stage: str, prefix: str, trigger_name: str) -> Change:
    """The category change for ``stage`` of trigger ``trigger_name``.

    ``matched`` adds the trigger tag; ``awaiting_claim`` adds the tag and the
    status in one write (a claim trigger never reports a separate ``matched``);
    the outcome stages set the status; ``skipped`` sets a weak status;
    ``reply_drafted`` adds its tag. An unknown stage changes nothing."""
    tag = trigger_category(prefix, trigger_name)
    if stage == MATCHED:
        return Change(add=(tag,), colors={tag: TRIGGER_COLOR})
    if stage not in _LABELS:
        return Change()
    name = category(prefix, _LABELS[stage])
    color = _STAGE_COLORS[stage]
    if stage == REPLY_DRAFTED:
        return Change(add=(name,), colors={name: color})
    if stage == AWAITING_CLAIM:
        return Change(add=(tag,), status=name, colors={tag: TRIGGER_COLOR, name: color})
    return Change(status=name, weak=stage == SKIPPED, colors={name: color})


def split(value: Any) -> list[str]:
    """Outlook's comma-joined ``Categories`` string → names (``;`` accepted too)."""
    text = str(value or "").replace(";", ",")
    return [c.strip() for c in text.split(",") if c.strip()]


def merge(
    existing: Any,
    *,
    add: tuple[str, ...] | list[str] = (),
    status: str = "",
    statuses: tuple[str, ...] | list[str] = (),
    weak: bool = False,
    remove: tuple[str, ...] | list[str] = (),
) -> str:
    """Apply a change to an existing ``Categories`` string; returns the new one.

    Drops ``remove``; then, for a ``status``, drops every name in ``statuses`` and
    appends it — unless ``weak`` and one of ``statuses`` is already there, which
    leaves the status alone; then appends each ``add`` not already present.
    Comparison is case-insensitive and other categories keep their order."""
    names = split(existing)
    drop = {r.lower() for r in remove}
    names = [n for n in names if n.lower() not in drop]
    if status:
        status_set = {s.lower() for s in statuses} | {status.lower()}
        has_status = any(n.lower() in status_set for n in names)
        if not (weak and has_status):
            names = [n for n in names if n.lower() not in status_set]
            names.append(status)
    seen = {n.lower() for n in names}
    for name in add:
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return ", ".join(names)
