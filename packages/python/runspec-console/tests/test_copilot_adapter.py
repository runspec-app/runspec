"""
test_copilot_adapter.py — the GitHub Copilot provider (adapters/copilot.py).

Drives CopilotAdapter against a fake Copilot SDK whose sessions play a scripted
model: they stream text, ask for the console's tools through the registered
handlers (and wait for the results, as the real runtime does), and finish with
an idle event. No Copilot runtime, network or sign-in is involved.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from runspec_console.adapters import copilot as cp
from runspec_console.adapters.base import ToolCall, load_adapter
from runspec_console.adapters.testing import (
    assert_adapter_contract,
    assert_chat_response,
    assert_tool_turn,
)

TOOLS = [
    {
        "name": "disk_usage",
        "description": "Disk usage on a host",
        "input_schema": {"type": "object", "properties": {"host": {"type": "string"}}},
    }
]


# ── a fake Copilot SDK ───────────────────────────────────────────────────────


@dataclass
class DeltaData:
    delta_content: str
    message_id: str
    parent_tool_call_id: str | None = None


@dataclass
class Request:
    tool_call_id: str
    name: str
    arguments: Any = None


@dataclass
class MessageData:
    content: str
    message_id: str
    tool_requests: list[Request] | None = None
    parent_tool_call_id: str | None = None


@dataclass
class UsageData:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ErrorData:
    message: str


@dataclass
class IdleData:
    aborted: bool | None = None


@dataclass
class CustomToolRequest:
    tool_name: str = ""


@dataclass
class ShellRequest:
    full_command_text: str = ""


@dataclass
class Approve:
    pass


@dataclass
class Reject:
    feedback: str = ""


@dataclass
class Tool:
    name: str
    description: str
    handler: Any = None
    parameters: Any = None
    skip_permission: bool = False
    defer: Any = None


@dataclass
class ToolResult:
    text_result_for_llm: str = ""
    result_type: str = "success"


class ToolSet:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add_custom(self, name: str) -> ToolSet:
        self.items.append(f"custom:{name}")
        return self


@dataclass
class Event:
    data: Any
    agent_id: str | None = None


@dataclass
class FakeSession:
    kwargs: dict[str, Any]
    brain: Any
    prompts: list[str] = field(default_factory=list)
    aborted: bool = False
    disconnected: bool = False
    tool_sets: list[list[str]] = field(default_factory=list)

    def emit(self, data: Any) -> None:
        self.kwargs["on_event"](Event(data))

    def tool(self, name: str) -> Any:
        return next(t for t in self.kwargs["tools"] if t.name == name)

    async def send(self, prompt: str) -> str:
        self.prompts.append(prompt)
        asyncio.get_running_loop().create_task(self.brain(self, prompt))
        return f"req-{len(self.prompts)}"

    async def abort(self) -> None:
        self.aborted = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def set_tools(self, tools: list[Any]) -> None:
        self.kwargs["tools"] = tools
        self.tool_sets.append([t.name for t in tools])


@dataclass
class ModelInfo:
    id: str
    name: str = ""
    capabilities: Any = None
    supported_reasoning_efforts: list[str] | None = None
    policy: Any = None
    billing: Any = None


def make_sdk(
    brain: Any, *, authenticated: bool = True, models: list[ModelInfo] | None = None
):
    state: dict[str, Any] = {"sessions": [], "clients": []}

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.started = False
            state["clients"].append(self)

        async def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.started = False

        async def list_models(self) -> list[ModelInfo]:
            return list(models or [])

        async def get_auth_status(self) -> Any:
            return SimpleNamespace(
                isAuthenticated=authenticated,
                login="octocat" if authenticated else None,
                host="https://github.com",
                statusMessage="" if authenticated else "Not authenticated",
            )

        async def create_session(self, **kwargs: Any) -> FakeSession:
            session = FakeSession(kwargs=kwargs, brain=brain)
            state["sessions"].append(session)
            return session

    sdk = SimpleNamespace(
        CopilotClient=FakeClient,
        RuntimeConnection=SimpleNamespace(
            for_stdio=lambda path=None: SimpleNamespace(path=path)
        ),
        ToolSet=ToolSet,
        Tool=Tool,
        ToolResult=ToolResult,
        AssistantMessageData=MessageData,
        AssistantMessageDeltaData=DeltaData,
        AssistantUsageData=UsageData,
        SessionErrorData=ErrorData,
        SessionIdleData=IdleData,
        PermissionRequestCustomTool=CustomToolRequest,
        Approve=Approve,
        Reject=Reject,
    )
    return sdk, state


async def tool_brain(session: FakeSession, prompt: str) -> None:
    """Asks for disk_usage once, then reports its result."""
    n = len(session.prompts)
    session.emit(DeltaData("Checking. ", f"m{n}a"))
    session.emit(
        MessageData(
            "Checking. ",
            f"m{n}a",
            [Request("call-1", "disk_usage", {"host": "web-01"})],
        )
    )
    session.emit(UsageData(input_tokens=100, output_tokens=10))
    result = await session.tool("disk_usage").handler(
        SimpleNamespace(
            tool_call_id="call-1", tool_name="disk_usage", arguments={"host": "web-01"}
        )
    )
    session.emit(DeltaData(f"Disk is {result.text_result_for_llm}.", f"m{n}b"))
    session.emit(MessageData(f"Disk is {result.text_result_for_llm}.", f"m{n}b"))
    session.emit(IdleData())


async def echo_brain(session: FakeSession, prompt: str) -> None:
    n = len(session.prompts)
    session.emit(DeltaData(f"echo {n}", f"m{n}"))
    session.emit(MessageData(f"echo {n}", f"m{n}"))
    session.emit(IdleData())


@pytest.fixture(autouse=True)
def _clean_runtimes():
    yield
    with cp._RUNTIMES_LOCK:
        runtimes = list(cp._RUNTIMES.values())
        cp._RUNTIMES.clear()
    for rt in runtimes:
        rt.submit(rt.shutdown()).result(timeout=5)


def adapter_for(sdk: Any, **kwargs: Any) -> cp.CopilotAdapter:
    return cp.CopilotAdapter(_sdk=sdk, workdir="/tmp", **kwargs)


def collect(adapter: cp.CopilotAdapter, messages: list[dict], tools=TOOLS):
    async def run() -> tuple[str, Any]:
        text = ""
        done = None
        async for kind, value in adapter.stream_with_tools(messages, tools):
            if kind == "text":
                text += value
            elif kind == "done":
                done = value
        return text, done

    # Each console turn runs in its own asyncio.run — so do these.
    return asyncio.run(run())


# ── the adapter ──────────────────────────────────────────────────────────────


def test_contract():
    sdk, _ = make_sdk(echo_brain)
    assert_adapter_contract(adapter_for(sdk))


def test_plain_reply_then_follow_up_reuses_the_session():
    sdk, state = make_sdk(echo_brain)
    adapter = adapter_for(sdk, system="Be brief.")
    history = [{"role": "user", "content": "hello"}]
    text, resp = collect(adapter, history)
    assert text == "echo 1"
    assert_chat_response(resp, expect_tool_calls=False)
    assert resp.stop_reason == "end_turn"

    # The console appends the reply, then the next user message.
    history += [
        {"role": "assistant", "content": text},
        {"role": "user", "content": "again"},
    ]
    # A new adapter object (the console rebuilds them) still finds the session.
    text2, _ = collect(adapter_for(sdk, system="Be brief."), history)
    assert text2 == "echo 2"
    assert len(state["sessions"]) == 1
    assert state["sessions"][0].prompts == ["hello", "again"]


def test_session_is_locked_down_to_console_tools():
    sdk, state = make_sdk(echo_brain)
    collect(
        adapter_for(sdk, system="SYS", model="gpt-5"),
        [{"role": "user", "content": "hi"}],
    )
    kw = state["sessions"][0].kwargs
    assert kw["available_tools"].items == ["custom:*"]
    assert kw["system_message"] == {"mode": "replace", "content": "SYS"}
    assert kw["model"] == "gpt-5"
    assert kw["skip_custom_instructions"] is True
    assert kw["enable_skills"] is False
    assert kw["memory"] == {"enabled": False}
    (tool,) = kw["tools"]
    assert tool.name == "disk_usage" and tool.skip_permission and tool.defer == "never"
    assert tool.parameters == TOOLS[0]["input_schema"]
    assert "reasoning_effort" not in kw
    on_permission = kw["on_permission_request"]
    assert isinstance(on_permission(CustomToolRequest("disk_usage"), None), Approve)
    assert isinstance(on_permission(ShellRequest("rm -rf /"), None), Reject)


def test_tool_round_trip_continues_the_same_copilot_turn():
    sdk, state = make_sdk(tool_brain)
    adapter = adapter_for(sdk)
    history: list[dict] = [{"role": "user", "content": "disk on web-01?"}]
    text, resp = collect(adapter, history)
    assert text == "Checking. "
    assert_chat_response(resp, expect_tool_calls=True)
    assert resp.tool_calls == [ToolCall("call-1", "disk_usage", {"host": "web-01"})]

    turn = adapter.make_tool_turn(resp, [(resp.tool_calls[0], "42%")])
    assert_tool_turn(turn)
    history += turn
    text2, resp2 = collect(adapter_for(sdk), history)
    assert text2 == "Disk is 42%."
    assert resp2.stop_reason == "end_turn"
    session = state["sessions"][0]
    assert len(state["sessions"]) == 1
    assert session.prompts == ["disk on web-01?"]  # one Copilot request for the turn


def test_edited_history_starts_a_new_session_with_a_transcript():
    sdk, state = make_sdk(echo_brain)
    adapter = adapter_for(sdk)
    collect(adapter, [{"role": "user", "content": "first"}])
    # Compaction / an excluded turn: the start of the history changed.
    history = [
        {"role": "user", "content": "summary of earlier work"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "next"},
    ]
    collect(adapter, history)
    assert len(state["sessions"]) == 2
    prompt = state["sessions"][1].prompts[0]
    assert "User: summary of earlier work" in prompt
    assert "Assistant: ok" in prompt
    assert prompt.rstrip().endswith("next")


def test_stopped_tool_turn_is_closed_when_the_user_moves_on():
    sdk, state = make_sdk(tool_brain)
    adapter = adapter_for(sdk)
    history: list[dict] = [{"role": "user", "content": "disk?"}]
    _, resp = collect(adapter, history)
    assert resp.tool_calls
    # The operator stopped before the tool ran; the console keeps only the text.
    history += [
        {"role": "assistant", "content": "Checking. "},
        {"role": "user", "content": "never mind"},
    ]
    collect(adapter, history)
    first, second = state["sessions"]
    assert first.aborted and first.disconnected
    assert second.prompts[0].rstrip().endswith("never mind")


def test_sign_in_error_explains_how_to_sign_in():
    async def unauth(session: FakeSession, prompt: str) -> None:
        session.emit(ErrorData("Not authenticated. Please authenticate first."))
        session.emit(IdleData())

    sdk, _ = make_sdk(unauth)
    with pytest.raises(RuntimeError) as err:
        collect(adapter_for(sdk), [{"role": "user", "content": "hi"}])
    assert "GitHub Copilot: Not authenticated" in str(err.value)
    assert "copilot login" in str(err.value)
    # The failed runtime is replaced on the next call (a new login is picked up).
    old = next(iter(cp._RUNTIMES.values()))
    assert old.stale
    adapter_for(sdk)._runtime()
    assert next(iter(cp._RUNTIMES.values())) is not old


def test_token_and_cli_path_reach_the_client():
    sdk, state = make_sdk(echo_brain)
    collect(
        adapter_for(sdk, api_key="ghp_x", cli_path="/opt/copilot"),
        [{"role": "user", "content": "hi"}],
    )
    (client,) = state["clients"]
    assert client.kwargs["github_token"] == "ghp_x"
    assert client.kwargs["connection"].path == "/opt/copilot"


def test_token_command_runs_once_per_runtime():
    sdk, state = make_sdk(echo_brain)
    with patch.object(cp, "run_api_key_command", return_value="gho_cmd") as run:
        collect(
            adapter_for(sdk, api_key_command="gh auth token"),
            [{"role": "user", "content": "a"}],
        )
        collect(
            adapter_for(sdk, api_key_command="gh auth token"),
            [{"role": "user", "content": "b"}],
        )
    assert run.call_count == 1
    assert state["clients"][0].kwargs["github_token"] == "gho_cmd"


def test_effort_only_sent_when_the_model_takes_it():
    caps = SimpleNamespace(supports=SimpleNamespace(reasoning_effort=True), limits=None)
    models = [
        ModelInfo(
            "gpt-5", capabilities=caps, supported_reasoning_efforts=["low", "high"]
        ),
        ModelInfo(
            "plain",
            capabilities=SimpleNamespace(
                supports=SimpleNamespace(reasoning_effort=False)
            ),
        ),
    ]
    sdk, state = make_sdk(echo_brain, models=models)
    collect(
        adapter_for(sdk, model="gpt-5", effort="high"),
        [{"role": "user", "content": "a"}],
    )
    collect(
        adapter_for(sdk, model="gpt-5", effort="medium"),
        [{"role": "user", "content": "b"}],
    )
    collect(
        adapter_for(sdk, model="plain", effort="high"),
        [{"role": "user", "content": "c"}],
    )
    efforts = [s.kwargs.get("reasoning_effort") for s in state["sessions"]]
    assert efforts == ["high", None, None]


def test_status_reports_login_and_models():
    billing = SimpleNamespace(multiplier=1.0)
    models = [
        ModelInfo(
            "claude-sonnet-4.6",
            "Claude Sonnet 4.6",
            policy=SimpleNamespace(state="enabled"),
            billing=billing,
        )
    ]
    sdk, _ = make_sdk(echo_brain, models=models)
    status = adapter_for(sdk).status()
    assert status["ok"] and status["login"] == "octocat"
    assert status["models"] == [
        {
            "id": "claude-sonnet-4.6",
            "name": "Claude Sonnet 4.6",
            "enabled": True,
            "multiplier": 1.0,
        }
    ]

    sdk2, _ = make_sdk(echo_brain, authenticated=False)
    status2 = adapter_for(sdk2).status()
    assert not status2["ok"] and status2["message"] == "Not authenticated"


def test_capabilities_use_the_models_limits():
    limits = SimpleNamespace(
        max_context_window_tokens=200_000, max_output_tokens=32_000
    )
    caps = SimpleNamespace(
        supports=SimpleNamespace(reasoning_effort=False), limits=limits
    )
    sdk, _ = make_sdk(echo_brain, models=[ModelInfo("m1", capabilities=caps)])
    adapter = adapter_for(sdk, model="m1")
    collect(adapter, [{"role": "user", "content": "a"}])
    got = adapter.capabilities()
    assert got.context_window == 200_000
    assert got.max_output_tokens == 32_000


def test_missing_sdk_names_the_extra():
    with patch.object(cp, "_load_sdk", side_effect=ImportError("no copilot")):
        with pytest.raises(ImportError, match=r"runspec-console\[copilot\]"):
            load_adapter("copilot")


# ── history helpers ──────────────────────────────────────────────────────────


def test_tool_results_reads_anthropic_and_openai_shapes():
    tail = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "a", "name": "x", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "a", "content": "ok"}],
        },
        {"role": "tool", "tool_call_id": "b", "content": "fine"},
    ]
    assert cp._tool_results(tail) == {"a": "ok", "b": "fine"}
    assert (
        cp._tool_results(tail + [{"role": "user", "content": "new question"}]) is None
    )


def test_fresh_prompt_renders_other_providers_tool_turns():
    messages = [
        {"role": "user", "content": "check"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "t1",
                    "function": {"name": "ping", "arguments": json.dumps({"h": 1})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "pong"},
    ]
    prompt = cp._fresh_prompt(messages)
    assert "Assistant called tool ping (t1)" in prompt
    assert "Tool result (t1):\npong" in prompt
    assert prompt.endswith("Carry on from where the conversation above stopped.")
    assert cp._fresh_prompt([{"role": "user", "content": "just this"}]) == "just this"


# ── cli_path: variables and npm installs ──────────────────────────────────────


def test_expand_path_handles_windows_and_posix_variables(tmp_path):
    env = {"AppData": "C:/Users/jo/AppData/Roaming", "HOME_DIR": "/home/jo"}
    assert (
        cp.expand_path("%APPDATA%/npm/copilot.cmd", env)
        == "C:/Users/jo/AppData/Roaming/npm/copilot.cmd"
    )
    assert cp.expand_path("$HOME_DIR/bin/copilot", env) == "/home/jo/bin/copilot"
    assert cp.expand_path("${HOME_DIR}/x", env) == "/home/jo/x"
    # An unknown variable is left alone rather than blanked.
    assert cp.expand_path("%NOPE%/copilot", env) == "%NOPE%/copilot"
    assert cp.expand_path("  ", env) == ""


def _npm_prefix(tmp_path):
    prefix = tmp_path / "npm"
    package = prefix / "node_modules" / "@github" / "copilot"
    package.mkdir(parents=True)
    (package / "package.json").write_text('{"bin": {"copilot": "npm-loader.js"}}')
    (package / "npm-loader.js").write_text("// loader\n")
    (prefix / "copilot.cmd").write_text("@echo off\n")
    (prefix / "copilot").write_text("#!/bin/sh\n")
    return prefix, package / "npm-loader.js"


def test_resolve_cli_path_turns_an_npm_install_into_its_js_entry(tmp_path):
    prefix, entry = _npm_prefix(tmp_path)
    env = {"APPDATA": str(tmp_path)}
    for raw in (
        "%APPDATA%/npm/copilot.cmd",
        "%APPDATA%/npm/copilot",
        "%APPDATA%/npm",
        str(prefix / "node_modules" / "@github" / "copilot"),
    ):
        assert cp.resolve_cli_path(raw, env) == str(entry), raw


def test_resolve_cli_path_leaves_other_paths_as_they_are(tmp_path):
    native = tmp_path / "copilot"
    native.write_text("binary")
    assert cp.resolve_cli_path(str(native), {}) == str(native)
    assert cp.resolve_cli_path("/opt/copilot/index.js", {}) == "/opt/copilot/index.js"
    assert cp.resolve_cli_path("", {}) == ""


def test_adapter_starts_the_resolved_runtime(tmp_path, monkeypatch):
    _prefix, entry = _npm_prefix(tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    sdk, state = make_sdk(echo_brain)
    adapter = adapter_for(sdk, cli_path="%APPDATA%/npm/copilot.cmd")
    assert adapter._cli_path == str(entry)
    adapter.status()
    assert state["clients"][-1].kwargs["connection"].path == str(entry)


# ── the console's [http] proxy reaches the runtime ──────────────────────────


def _with_proxy(monkeypatch, settings):
    from runspec_console import netproxy

    monkeypatch.setattr(netproxy, "_current", settings)


def test_runtime_gets_the_console_manual_proxy(monkeypatch):
    from runspec_console import netproxy

    _with_proxy(
        monkeypatch,
        netproxy.ProxySettings(proxy="http://proxy.corp:8080", no_proxy=("intra",)),
    )
    monkeypatch.setenv("KEEP_ME", "1")
    sdk, state = make_sdk(echo_brain)
    adapter_for(sdk).status()
    env = state["clients"][-1].kwargs["env"]
    assert env["HTTPS_PROXY"] == "http://proxy.corp:8080"
    assert env["http_proxy"] == "http://proxy.corp:8080"
    assert "intra" in env["NO_PROXY"]
    assert env["KEEP_ME"] == "1"  # the rest of the environment is inherited


def test_runtime_direct_removes_inherited_proxy(monkeypatch):
    from runspec_console import netproxy

    _with_proxy(monkeypatch, netproxy.ProxySettings(proxy=netproxy.DIRECT))
    monkeypatch.setenv("HTTPS_PROXY", "http://machine:3128")
    sdk, state = make_sdk(echo_brain)
    adapter_for(sdk).status()
    env = state["clients"][-1].kwargs["env"]
    assert "HTTPS_PROXY" not in env and "https_proxy" not in env
    assert "PATH" in env or "Path" in env


def test_runtime_system_proxy_inherits(monkeypatch):
    from runspec_console import netproxy

    _with_proxy(monkeypatch, netproxy.ProxySettings())
    sdk, state = make_sdk(echo_brain)
    adapter_for(sdk).status()
    assert "env" not in state["clients"][-1].kwargs


def test_changing_the_proxy_starts_a_new_runtime(monkeypatch):
    from runspec_console import netproxy

    sdk, state = make_sdk(echo_brain)
    adapter = adapter_for(sdk)
    _with_proxy(monkeypatch, netproxy.ProxySettings(proxy="http://a:1"))
    first = adapter._runtime()
    assert adapter._runtime() is first
    _with_proxy(monkeypatch, netproxy.ProxySettings(proxy="http://b:2"))
    assert adapter._runtime() is not first


# ── usage: cache split + AI credits / premium requests ──────────────────────


def test_add_usage_splits_out_cache_and_reads_billing():
    usage = cp.new_usage()
    cp.add_usage(
        usage,
        SimpleNamespace(
            input_tokens=1000,
            output_tokens=50,
            cache_read_tokens=800,
            cache_write_tokens=100,
            copilot_usage=SimpleNamespace(total_nano_aiu=420_000_000.0),
            cost=1.0,
        ),
    )
    cp.add_usage(usage, SimpleNamespace(input_tokens=200, output_tokens=5, cost=0.0))
    assert usage.input_tokens == 100 + 200  # uncached prompt only
    assert usage.cache_read_input_tokens == 800
    assert usage.cache_creation_input_tokens == 100
    assert usage.output_tokens == 55
    assert usage.ai_credits == pytest.approx(0.42)
    assert usage.premium_requests == 1.0


def test_add_usage_never_goes_negative():
    usage = cp.new_usage()
    cp.add_usage(usage, SimpleNamespace(input_tokens=10, cache_read_tokens=50))
    assert usage.input_tokens == 0 and usage.cache_read_input_tokens == 50


def test_console_reads_copilot_usage_and_billing():
    from runspec_console.bridge import Bridge
    from runspec_console.bridge_turn import billing_from_response

    usage = cp.new_usage()
    cp.add_usage(
        usage,
        SimpleNamespace(
            input_tokens=1000,
            output_tokens=50,
            cache_read_tokens=900,
            copilot_usage=SimpleNamespace(total_nano_aiu=2_000_000_000),
            cost=0.33,
        ),
    )
    response = SimpleNamespace(_raw=SimpleNamespace(usage=usage))
    assert Bridge._usage_from_response(response) == {
        "input": 100,
        "output": 50,
        "cache_read": 900,
        "cache_creation": 0,
    }
    assert billing_from_response(response) == {
        "ai_credits": 2.0,
        "premium_requests": 0.33,
    }
    plain = SimpleNamespace(_raw=SimpleNamespace(usage=SimpleNamespace(input_tokens=5)))
    assert billing_from_response(plain) == {}
