import asyncio
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

import tomllib

import chainlit as cl
from chainlit.types import CommandDict
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from runspec_chat.adapter import ChatResponse, ToolCall
from runspec_chat.adapters.anthropic_direct import DEFAULT_SYSTEM, AnthropicAdapter
from runspec_chat.chat import (
    _host_pass_key,
    _resolve_hosts_path,
    _shared_pass_key,
    _sync_user_env,
)

_LOCAL_CONN = "__runspec_local__"
_DEFAULT_MODEL = os.environ.get("RUNSPEC_CHAT_MODEL", "claude-haiku-4-5-20251001")
_SELF_TOOLS = {"runspec-chat", "setup-keys"}  # hide from "Local tools ready" message
_COMMANDS_HIDE = {"runspec-chat"}  # hide from slash-command autocomplete

_HOSTS_PATH = Path(
    os.environ.get("RUNSPEC_CHAT_HOSTS", "~/.config/runspec-chat/jump_hosts.toml")
).expanduser()

_chainlit_root = Path(os.environ.get("CHAINLIT_ROOT", Path(__file__).parent))
_sync_user_env(
    hosts_path=_HOSTS_PATH,
    chainlit_config=_chainlit_root / ".chainlit" / "config.toml",
)


def _load_host_categories() -> dict[str, str]:
    """Return {host_name: category} from jump_hosts.toml. Falls back to host name."""
    resolved = _resolve_hosts_path(_HOSTS_PATH)
    if not resolved.exists():
        return {}
    try:
        with open(resolved, "rb") as f:
            cfg = tomllib.load(f)
        return {
            name: info.get("category", name)
            for name, info in cfg.get("hosts", {}).items()
        }
    except Exception:
        return {}


_HOST_CATEGORIES: dict[str, str] = _load_host_categories()


def _get_user_identity() -> tuple[str, str | None]:
    """Return (login, display_name). display_name is None if OS username is all that's known."""
    login = (
        os.environ.get("USER")
        or os.environ.get("LOGNAME")
        or os.environ.get("USERNAME")
        or "unknown"
    )
    if sys.platform == "win32":
        try:
            import win32api  # type: ignore[import-untyped]
            import win32con  # type: ignore[import-untyped]

            name = win32api.GetUserNameEx(win32con.NameDisplay)
            if name and name != login:
                return login, name
        except Exception:
            pass
    else:
        try:
            import pwd

            gecos = pwd.getpwuid(os.getuid()).pw_gecos
            name = gecos.split(",")[0].strip()
            if name and name != login:
                return login, name
        except (ImportError, KeyError, AttributeError):
            pass
    return login, None


def _format_user(login: str, display_name: str | None) -> str:
    return f"{display_name} ({login})" if display_name else login


_USER_LOGIN, _USER_DISPLAY_NAME = _get_user_identity()


async def _refresh_commands() -> None:
    # Local tools stored separately so they survive mcp_tools session resets
    local_tools: list = cl.user_session.get("local_tools", [])
    mcp_tools: dict = cl.user_session.get("mcp_tools", {})

    seen: set[str] = set()
    commands: list[CommandDict] = []

    for t in local_tools:
        if t["name"] in _COMMANDS_HIDE or t["name"] in seen:
            continue
        seen.add(t["name"])
        base_desc = t.get("description") or t["name"]
        icon = "key" if t["name"] == "setup-keys" else "terminal"
        commands.append(
            {
                "id": t["name"],
                "description": f"[local] {base_desc}",
                "icon": icon,
                "button": False,
                "persistent": None,
                "selected": None,
            }
        )

    for conn, tools in mcp_tools.items():
        if conn == _LOCAL_CONN:
            continue
        category = _HOST_CATEGORIES.get(conn, conn)
        for t in tools:
            if t["name"] in _COMMANDS_HIDE or t["name"] in seen:
                continue
            seen.add(t["name"])
            base_desc = t.get("description") or t["name"]
            commands.append(
                {
                    "id": t["name"],
                    "description": f"[{category}] {base_desc}",
                    "icon": "terminal",
                    "button": False,
                    "persistent": None,
                    "selected": None,
                }
            )

    try:
        await cl.context.emitter.set_commands(commands)
    except Exception as exc:
        await cl.Message(content=f"⚠ Could not update command list: {exc}").send()


def _local_runspec_exe() -> str:
    """Return the runspec executable in the same venv as this process."""
    exe = Path(sys.executable).parent / "runspec"
    return str(exe) if exe.exists() else "runspec"


# ---------------------------------------------------------------------------
# MCP connection lifecycle  (user-initiated via Chainlit plug icon)
# ---------------------------------------------------------------------------


@cl.on_mcp_connect
async def on_mcp_connect(connection, session: ClientSession) -> None:
    result = await session.list_tools()
    tools = [
        {"name": t.name, "description": t.description, "input_schema": t.inputSchema}
        for t in result.tools
    ]
    mcp_tools: dict = cl.user_session.get("mcp_tools", {})
    mcp_tools[connection.name] = tools
    cl.user_session.set("mcp_tools", mcp_tools)
    tool_names: list[str] = [str(t["name"]) for t in tools]
    category = _HOST_CATEGORIES.get(connection.name, connection.name)
    await cl.Message(
        content=f"── {category} ──\n✓ Connected to **{connection.name}** — {len(tools)} tool(s): `{'`, `'.join(tool_names)}`"
    ).send()
    await _refresh_commands()


@cl.on_mcp_disconnect
async def on_mcp_disconnect(name: str, session: ClientSession) -> None:
    mcp_tools: dict = cl.user_session.get("mcp_tools", {})
    mcp_tools.pop(name, None)
    cl.user_session.set("mcp_tools", mcp_tools)
    await _refresh_commands()


# ---------------------------------------------------------------------------
# Chat lifecycle
# ---------------------------------------------------------------------------


@cl.on_chat_start
async def on_chat_start() -> None:
    cl.user_session.set("messages", [])
    cl.user_session.set("mcp_tools", {})
    cl.user_session.set("local_sessions", {})
    cl.user_session.set(
        "user_identity", {"login": _USER_LOGIN, "display_name": _USER_DISPLAY_NAME}
    )

    # API key: browser settings take precedence over .env
    env = cl.user_session.get("env") or {}
    api_key = env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        await cl.Message(
            content="No API key found. Open **Settings** (⚙ gear icon) and enter your `ANTHROPIC_API_KEY`."
        ).send()
    user_str = _format_user(_USER_LOGIN, _USER_DISPLAY_NAME)
    system = DEFAULT_SYSTEM + f"\nSession user: {user_str}."
    cl.user_session.set(
        "adapter",
        AnthropicAdapter(model=_DEFAULT_MODEL, api_key=api_key or None, system=system),
    )

    await _connect_local()


@cl.on_chat_end
async def on_chat_end() -> None:
    stop: asyncio.Event | None = cl.user_session.get("local_stop")
    if stop:
        stop.set()
    task: asyncio.Task | None = cl.user_session.get("local_task")
    if task:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except Exception:
            task.cancel()


async def _local_mcp_task(
    session_holder: list,
    tools_holder: list,
    ready: asyncio.Event,
    stop: asyncio.Event,
) -> None:
    """Runs the full local MCP session in one task so anyio cancel scopes are
    entered and exited in the same task — required by anyio's task model."""
    params = StdioServerParameters(command=_local_runspec_exe(), args=["serve"])
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                tools_holder.extend(
                    {
                        "name": t.name,
                        "description": t.description,
                        "input_schema": t.inputSchema,
                    }
                    for t in result.tools
                )
                session_holder.append(session)
                ready.set()
                await stop.wait()
    except Exception:
        ready.set()  # unblock _connect_local even on failure


async def _connect_local() -> None:
    session_holder: list = []
    tools_holder: list = []
    ready = asyncio.Event()
    stop = asyncio.Event()

    task = asyncio.create_task(
        _local_mcp_task(session_holder, tools_holder, ready, stop)
    )
    cl.user_session.set("local_stop", stop)
    cl.user_session.set("local_task", task)

    try:
        await asyncio.wait_for(asyncio.shield(ready.wait()), timeout=5.0)
    except asyncio.TimeoutError:
        await cl.Message(content="⚠ Local tools timed out on startup.").send()
        return

    if not session_holder:
        await cl.Message(content="⚠ Could not start local tools.").send()
        return

    mcp_tools: dict = cl.user_session.get("mcp_tools", {})
    mcp_tools[_LOCAL_CONN] = tools_holder
    cl.user_session.set("mcp_tools", mcp_tools)
    cl.user_session.set("local_session", session_holder[0])
    cl.user_session.set("local_tools", tools_holder)  # survives mcp_tools resets

    user_tools = [t["name"] for t in tools_holder if t["name"] not in _SELF_TOOLS]
    user_str = _format_user(_USER_LOGIN, _USER_DISPLAY_NAME)
    if user_tools:
        await cl.Message(
            content=f"── local ──\n✓ Local tools ready: `{'`, `'.join(user_tools)}` | running as **{user_str}**"
        ).send()
    else:
        await cl.Message(
            content=f"── local ──\nReady. Connect a remote host via the **plug icon**, or type `/setup-keys` to set up SSH keys. | running as **{user_str}**"
        ).send()
    await _refresh_commands()


# ---------------------------------------------------------------------------
# Built-in: setup-keys  (runs in-process so it can use browser credentials)
# ---------------------------------------------------------------------------


async def _builtin_setup_keys(tool_input: dict) -> str:
    hosts_path = _resolve_hosts_path(
        Path(
            tool_input.get("hosts", "~/.config/runspec-chat/jump_hosts.toml")
        ).expanduser()
    )
    if not hosts_path.exists():
        return f"No hosts config at `{hosts_path}`. Copy `jump_hosts.toml.example` and edit it."

    with open(hosts_path, "rb") as f:
        config = tomllib.load(f)

    defaults = config.get("config", {})
    default_user = defaults.get("user")

    ssh_hosts = [
        (name, info)
        for name, info in config.get("hosts", {}).items()
        if info.get("ssh")
    ]
    if not ssh_hosts:
        return "No SSH hosts found in config (hosts with an `ssh` field)."

    # Resolve credentials — password comes from browser Settings (user_env)
    env_vals = cl.user_session.get("env") or {}
    resolved: list[tuple[str, str, str]] = []  # (name, target, password)
    missing: list[str] = []
    for name, info in ssh_hosts:
        user = info.get("user") or default_user
        target = f"{user}@{info['ssh']}" if user else info["ssh"]
        # Hosts with their own user get a dedicated key; others share SSH_PASS
        pass_key = _host_pass_key(name) if info.get("user") else _shared_pass_key()
        password = env_vals.get(pass_key)
        if not password:
            missing.append(f"  - `{name}`: enter `{pass_key}` in Settings (⚙)")
        else:
            resolved.append((name, target, password))

    if missing and not resolved:
        return "No passwords set. Open **Settings** (⚙) and fill in:\n" + "\n".join(
            missing
        )

    key_type = tool_input.get("key_type", "ed25519")
    key_path = Path.home() / ".ssh" / f"runspec-chat_{key_type}"
    pub_key = key_path.with_suffix(".pub")

    if not key_path.exists():
        async with cl.Step(name="ssh-keygen") as step:
            proc = await asyncio.create_subprocess_exec(
                "ssh-keygen",
                "-t",
                key_type,
                "-f",
                str(key_path),
                "-N",
                "",
                "-C",
                "runspec-chat",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                step.output = f"Failed: {stderr.decode()}"
                return step.output
            step.output = f"Created {pub_key}"

    is_windows = sys.platform == "win32"
    has_sshpass = not is_windows and bool(shutil.which("sshpass"))

    if is_windows or not has_sshpass:
        pub_key_content = pub_key.read_text().strip()
        host_lines = "\n".join(
            f"  ssh {target} \"cat >> ~/.ssh/authorized_keys\" << 'EOF'\n  {pub_key_content}\n  EOF"
            for _, target, _ in resolved
        )
        reason = (
            "Windows"
            if is_windows
            else "`sshpass` not installed (`sudo apt-get install sshpass`)"
        )
        return (
            f"Key generated at `{pub_key}` ✓\n\n"
            f"Automated copy unavailable ({reason}). "
            f"Run these commands once on the machine running runspec-chat:\n\n"
            f"```bash\n{host_lines}\n```\n\n"
            f"Or ask your admin to add `{pub_key}` to each host's `~/.ssh/authorized_keys`."
        )

    ok, failed = [], []
    for name, target, password in resolved:
        async with cl.Step(name=f"ssh-copy-id → {name}") as step:
            proc = await asyncio.create_subprocess_exec(
                "sshpass",
                "-e",
                "ssh-copy-id",
                "-i",
                str(pub_key),
                target,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "SSHPASS": password},
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                step.output = f"Failed: {stderr.decode().strip()}"
                failed.append(name)
            else:
                step.output = "Done"
                ok.append(name)

    lines = []
    if missing:
        lines.append(
            "Skipped (no password in Settings): "
            + ", ".join(f"`{m.split('`')[1]}`" for m in missing)
        )
    if ok:
        lines.append(f"{len(ok)} host(s) configured: {', '.join(f'`{n}`' for n in ok)}")
    if failed:
        lines.append(f"{len(failed)} failed: {', '.join(f'`{n}`' for n in failed)}")
    if ok:
        lines.append(
            f"\nAdd to `~/.ssh/config`:\n```\nHost *\n    IdentityFile ~/.ssh/runspec-chat_{key_type}\n```"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


def _get_session(mcp_name: str) -> ClientSession | None:
    """Return the ClientSession for a named connection, local or Chainlit-managed."""
    if mcp_name == _LOCAL_CONN:
        return cl.user_session.get("local_session")
    pair = getattr(cl.context.session, "mcp_sessions", {}).get(mcp_name)
    return pair.client if pair else None


@cl.step(type="tool")
async def call_tool(tool_call: ToolCall) -> str:
    step = cl.context.current_step
    step.name = tool_call.name
    step.input = tool_call.input

    # setup-keys runs in-process so it can access browser credentials
    if tool_call.name == "setup-keys":
        step.output = await _builtin_setup_keys(tool_call.input)
        return step.output

    mcp_tools: dict = cl.user_session.get("mcp_tools", {})
    mcp_name = next(
        (
            conn
            for conn, tools in mcp_tools.items()
            if any(t["name"] == tool_call.name for t in tools)
        ),
        None,
    )

    if not mcp_name:
        step.output = json.dumps(
            {"error": f"Tool '{tool_call.name}' not found in any connected MCP server"}
        )
        return step.output

    mcp_session = _get_session(mcp_name)
    if not mcp_session:
        step.output = json.dumps({"error": f"MCP session '{mcp_name}' unavailable"})
        return step.output

    try:
        raw = await mcp_session.call_tool(tool_call.name, tool_call.input)
        if hasattr(raw, "content") and raw.content:
            step.output = "\n".join(
                block.text for block in raw.content if hasattr(block, "text")
            )
        else:
            step.output = str(raw)
        rs_meta = (getattr(raw, "meta", None) or {}).get("runspec", {})
        if rs_meta.get("duration_ms") is not None:
            step.name = f"{tool_call.name} ({rs_meta['duration_ms']}ms)"
    except Exception as exc:
        step.output = json.dumps({"error": str(exc)})

    return step.output


# ---------------------------------------------------------------------------
# Slash command handler  (/toolname --arg value ...)
# ---------------------------------------------------------------------------


def _parse_slash(text: str) -> tuple[str, dict]:
    try:
        parts = shlex.split(text[1:])
    except ValueError:
        parts = text[1:].split()

    name = parts[0] if parts else ""
    args: dict = {}
    i = 1
    while i < len(parts):
        token = parts[i]
        if token.startswith("--"):
            key = token[2:].replace("-", "_")
            if i + 1 < len(parts) and not parts[i + 1].startswith("--"):
                args[key] = parts[i + 1]
                i += 2
            else:
                args[key] = True
                i += 1
        else:
            i += 1
    return name, args


async def _handle_slash(text: str) -> None:
    tool_name, tool_input = _parse_slash(text)
    if not tool_name:
        await cl.Message(content="Usage: `/tool_name --arg value`").send()
        return

    local_tools: list = cl.user_session.get("local_tools", [])
    mcp_tools: dict = cl.user_session.get("mcp_tools", {})
    all_tools = local_tools + [
        t for conn, tools in mcp_tools.items() for t in tools if conn != _LOCAL_CONN
    ]
    tool_def = next((t for t in all_tools if t["name"] == tool_name), None)

    if tool_def is None:
        known = sorted({t["name"] for t in all_tools})
        available = ", ".join(f"`{n}`" for n in known) if known else "none"
        await cl.Message(
            content=f"Unknown tool `{tool_name}`. Available: {available}"
        ).send()
        return

    if tool_input.get("help"):
        schema = tool_def.get("input_schema", {})
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        desc = tool_def.get("description") or ""
        lines = [f"**/{tool_name}** — {desc}" if desc else f"**/{tool_name}**"]
        if props:
            lines.append("\n**Arguments:**")
            for arg, info in props.items():
                req = " *(required)*" if arg in required else ""
                arg_desc = info.get("description", "")
                arg_type = info.get("type", "")
                lines.append(f"  `--{arg}`{req} `{arg_type}` — {arg_desc}")
        else:
            lines.append("No arguments.")
        await cl.Message(content="\n".join(lines)).send()
        return

    tc = ToolCall(id="slash-0", name=tool_name, input=tool_input)
    result = await call_tool(tc)
    content = result if isinstance(result, str) else str(result)
    await cl.Message(content=f"```\n{content.strip()}\n```").send()


# ---------------------------------------------------------------------------
# LLM message loop
# ---------------------------------------------------------------------------


async def _llm_loop(user_text: str) -> None:
    adapter: AnthropicAdapter | None = cl.user_session.get("adapter")
    if not adapter:
        await cl.Message(
            content="No LLM configured. Open **Settings** (⚙ gear icon) and enter your `ANTHROPIC_API_KEY`."
        ).send()
        return

    messages: list = cl.user_session.get("messages", [])
    messages.append({"role": "user", "content": user_text})

    mcp_tools: dict = cl.user_session.get("mcp_tools", {})
    tools = [t for conn_tools in mcp_tools.values() for t in conn_tools]

    response: ChatResponse = await adapter.chat(messages, tools)

    while response.stop_reason == "tool_use":
        results: list[tuple[ToolCall, str]] = []
        for tc in response.tool_calls:
            result = await call_tool(tc)
            results.append((tc, str(result)))

        messages.extend(adapter.make_tool_turn(response, results))
        response = await adapter.chat(messages, tools)

    reply = response.text or ""
    await cl.Message(content=reply).send()

    messages.append({"role": "assistant", "content": reply})
    cl.user_session.set("messages", messages)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@cl.on_message
async def on_message(msg: cl.Message) -> None:
    await _refresh_commands()
    text = msg.content.strip()
    if text.startswith("/"):
        await _handle_slash(text)
    else:
        await _llm_loop(text)
