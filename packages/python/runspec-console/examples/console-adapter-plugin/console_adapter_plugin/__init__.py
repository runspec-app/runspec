"""Example runspec-console LLM provider plugin.

Three ways the host (runspec-console) can find ``MyCorpAdapter`` — pick one:

1. Entry point (recommended): declared in ``pyproject.toml`` under the
   ``runspec_console.adapters`` group. ``pip install`` the wheel and set
   ``provider = "mycorp"``. Nothing else required.

2. register_adapter at import time: uncomment the block below, then list this
   module under ``[plugins] modules`` in the runspec-console config so it's
   imported at startup::

       [plugins]
       modules = ["console_adapter_plugin"]

3. Dotted path: skip packaging entirely and set
   ``provider = "console_adapter_plugin.adapter:MyCorpAdapter"`` directly.
"""

# from runspec_console import register_adapter
# from .adapter import MyCorpAdapter
#
# register_adapter("mycorp", MyCorpAdapter)
