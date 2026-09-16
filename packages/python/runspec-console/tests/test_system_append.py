"""Tests for ``[llm] system_append`` — additive standing instructions.

``[llm] system`` replaces the default base prompt wholesale, which silently costs
an operator the clarify-don't-guess guideline the moment they set any site policy
of their own. ``system_append`` is the additive alternative: it layers on top of
whichever base won, so "our policy *and* the built-in guidance" is expressible.
"""

from __future__ import annotations

import types

from runspec_console.bridge import Bridge
from runspec_console.bridge_const import DEFAULT_SYSTEM_PROMPT


def _stub(llm: dict) -> object:
    s = types.SimpleNamespace(_config={"llm": llm})
    s.get_config = lambda: s._config
    s._base_system = types.MethodType(Bridge._base_system, s)
    return s


def test_default_base_carries_the_clarify_guideline():
    text = _stub({})._base_system()
    assert "clarifying question" in text
    assert "runspec-console" in text


def test_configured_system_still_replaces_wholesale():
    """Unchanged behaviour — an existing operator's custom prompt must not start
    growing a preamble it never had."""
    text = _stub({"system": "Only speak in haiku."})._base_system()
    assert text == "Only speak in haiku."


def test_append_layers_on_the_default():
    text = _stub({"system_append": "Never touch prod before 09:00."})._base_system()
    assert "clarifying question" in text  # the default survives...
    assert text.endswith("Never touch prod before 09:00.")  # ...and policy follows


def test_append_layers_on_a_configured_system():
    text = _stub(
        {"system": "Only speak in haiku.", "system_append": "Never touch prod."}
    )._base_system()
    assert text == "Only speak in haiku.\n\nNever touch prod."


def test_blank_append_changes_nothing():
    """A blank/whitespace value must be byte-identical to unset — the composed
    prefix is prompt-cached, so an empty setting must not churn it."""
    assert _stub({"system_append": "   "})._base_system() == _stub({})._base_system()


def test_default_points_the_agent_at_runbooks():
    """Runbooks are the operator's standing procedures; the agent should prefer an
    existing one and offer to capture a correction that would generalise."""
    text = _stub({})._base_system()
    assert "runbook" in text.lower()
    assert "save_runbook" in text


def test_default_system_prompt_is_the_base_default():
    """The Settings pre-fill reads exactly what an unset [llm] system runs."""
    assert Bridge.default_system_prompt(object()) == DEFAULT_SYSTEM_PROMPT
    assert _stub({})._base_system() == DEFAULT_SYSTEM_PROMPT


def test_configured_system_equal_to_default_is_byte_identical():
    assert (
        _stub({"system": DEFAULT_SYSTEM_PROMPT})._base_system()
        == _stub({})._base_system()
    )
