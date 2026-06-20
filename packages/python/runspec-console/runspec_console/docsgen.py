"""
docsgen.py — a deterministic markdown runbook wiki from skills + hosts.

Skills (``runspec_skills.toml``) are written for the *agent* (step templates that
name runnables). This renders them as a human-followable **wiki** so that during
an outage — when the console itself may be down — a person can SSH to the host and
run the same runspec runnables the agent would have.

``render_wiki(skills, hosts) -> {relpath: markdown}`` is **pure** and
**deterministic**: same skills/hosts in → byte-identical markdown out (everything
sorted, no timestamps), so the generated files diff cleanly when committed back to
the config repo and a daily regenerate is a no-op unless the skills actually
changed.

Output:
  * ``README.md``      — index: an outage preamble, skills grouped by tag, host list
  * ``skills/<id>.md`` — one page per skill: when-to-use, steps, params, cross-links
  * ``hosts.md``       — hosts → ssh target → which skills reference each

The skill dict shape is what ``memory.load_skills`` returns
(``id``/``title``/``symptom``/``steps``/``tags``/``args``); host dicts are fleet
entries (``name``/``ssh``/``tags``).
"""

from __future__ import annotations

import re
from typing import Any

# host__runnable tool tokens an agent would call, spotted in a skill's free-text
# steps so the wiki can cross-link them (deterministic given the steps text).
_RUNNABLE_TOKEN = re.compile(r"\b[A-Za-z0-9][\w-]*__[A-Za-z0-9][\w-]*\b")
_UNSAFE = re.compile(r"[^a-z0-9_-]+")


def render_wiki(
    skills: list[dict[str, Any]], hosts: list[dict[str, Any]]
) -> dict[str, str]:
    """Render the whole wiki as ``{relative_path: markdown_text}``."""
    skills = sorted((s for s in skills if s.get("id")), key=lambda s: str(s.get("id")))
    host_names = sorted({str(h.get("name")) for h in hosts if h.get("name")})

    out: dict[str, str] = {"README.md": _render_index(skills, host_names)}
    for skill in skills:
        out[f"skills/{_safe(str(skill['id']))}.md"] = _render_skill(skill, host_names)
    out["hosts.md"] = _render_hosts(hosts, skills)
    return out


# ── pages ─────────────────────────────────────────────────────────────────────


def _render_index(skills: list[dict[str, Any]], host_names: list[str]) -> str:
    lines = [
        "# Runbook wiki",
        "",
        "Human-followable runbooks generated from the team's skills. **If the "
        "console is unavailable**, pick the runbook for your situation below, then "
        "SSH to the relevant host and run the runspec runnable its steps name — the "
        "same one the agent would have run:",
        "",
        "```",
        "ssh <host>            # see hosts.md for the SSH target",
        "runspec local         # list the runnables available on that host",
        "runspec run <runnable> --help",
        "```",
        "",
        "## Runbooks by tag",
        "",
    ]
    by_tag: dict[str, list[dict[str, Any]]] = {}
    for skill in skills:
        tags = [str(t).strip() for t in (skill.get("tags") or []) if str(t).strip()]
        for tag in tags or ["Untagged"]:
            by_tag.setdefault(tag, []).append(skill)

    if not skills:
        lines.append("_No skills defined yet._")
    for tag in sorted(by_tag, key=lambda t: (t == "Untagged", t.lower())):
        lines.append(f"### {tag}")
        lines.append("")
        for skill in by_tag[tag]:
            sid = _safe(str(skill["id"]))
            title = str(skill.get("title") or skill["id"])
            symptom = str(skill.get("symptom") or "").strip()
            suffix = f" — {symptom}" if symptom else ""
            lines.append(f"- [{title}](skills/{sid}.md){suffix}")
        lines.append("")

    lines.append("## Hosts")
    lines.append("")
    if host_names:
        lines.append(f"{len(host_names)} host(s) — see [hosts.md](hosts.md).")
    else:
        lines.append("_No hosts defined._")
    lines.append("")
    return "\n".join(lines)


def _render_skill(skill: dict[str, Any], host_names: list[str]) -> str:
    title = str(skill.get("title") or skill["id"])
    lines = [f"# {title}", ""]

    symptom = str(skill.get("symptom") or "").strip()
    if symptom:
        lines += [f"**When to use:** {symptom}", ""]

    tags = sorted({str(t).strip() for t in (skill.get("tags") or []) if str(t).strip()})
    if tags:
        lines += ["**Tags:** " + ", ".join(f"`{t}`" for t in tags), ""]

    steps = str(skill.get("steps") or "").strip()
    lines += ["## Steps", "", steps or "_No steps recorded._", ""]

    params = _params_table(skill.get("args"))
    if params:
        lines += ["## Parameters", "", *params, ""]

    refs_runnables = sorted(set(_RUNNABLE_TOKEN.findall(steps)))
    if refs_runnables:
        lines += ["## Runnables referenced", ""]
        lines += [f"- `{r}`" for r in refs_runnables]
        lines.append("")

    refs_hosts = _referenced_hosts(steps, host_names)
    if refs_hosts:
        lines += ["## Hosts referenced", ""]
        lines += [f"- [`{h}`](../hosts.md)" for h in refs_hosts]
        lines.append("")

    return "\n".join(lines)


def _render_hosts(hosts: list[dict[str, Any]], skills: list[dict[str, Any]]) -> str:
    lines = ["# Hosts", ""]
    rows = sorted(
        ((str(h.get("name")), h) for h in hosts if h.get("name")),
        key=lambda r: r[0],
    )
    if not rows:
        return "# Hosts\n\n_No hosts defined._\n"

    # Reverse map: host name → skills whose steps reference it.
    used_by: dict[str, list[str]] = {}
    for skill in skills:
        steps = str(skill.get("steps") or "")
        for name in _referenced_hosts(steps, [n for n, _ in rows]):
            used_by.setdefault(name, []).append(str(skill.get("title") or skill["id"]))

    lines += ["| Host | SSH target | Tags | Used by runbooks |", "|---|---|---|---|"]
    for name, h in rows:
        ssh = _cell(str(h.get("ssh") or ""))
        tags = ", ".join(
            str(t).strip() for t in (h.get("tags") or []) if str(t).strip()
        )
        runbooks = ", ".join(sorted(used_by.get(name, []))) or "—"
        lines.append(
            f"| `{name}` | {ssh or '—'} | {_cell(tags) or '—'} | {_cell(runbooks)} |"
        )
    lines.append("")
    return "\n".join(lines)


# ── helpers ─────────────────────────────────────────────────────────────────────


def _params_table(args_table: dict[str, Any] | None) -> list[str]:
    """A skill's ``[skill.args]`` rendered as a markdown table (or [] when none)."""
    if not args_table:
        return []
    from .skillargs import parse_skill_args

    defs = parse_skill_args(args_table)
    if not defs:
        return []
    rows = [
        "| Name | Type | Required | Default | Description |",
        "|---|---|---|---|---|",
    ]
    for d in defs:
        name = _cell(str(d.get("name", "")))
        typ = _cell(str(d.get("type", "")))
        if d.get("options"):
            typ += " (" + ", ".join(str(o) for o in d["options"]) + ")"
        required = "yes" if d.get("required") else "no"
        default = "" if d.get("default") is None else str(d.get("default"))
        desc = _cell(str(d.get("description", "")))
        rows.append(
            f"| `{name}` | {_cell(typ)} | {required} | {_cell(default) or '—'} | {desc or '—'} |"
        )
    return rows


def _referenced_hosts(steps: str, host_names: list[str]) -> list[str]:
    """Host names that appear as whole words in ``steps`` (sorted, deduped)."""
    found = []
    for name in sorted(set(host_names)):
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", steps):
            found.append(name)
    return found


def _cell(text: str) -> str:
    """Escape a value for a markdown table cell (pipes, newlines)."""
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def _safe(skill_id: str) -> str:
    """A filesystem-safe slug for a skill id (ids are already slugs; belt + braces)."""
    return _UNSAFE.sub("-", skill_id.lower()).strip("-") or "skill"
