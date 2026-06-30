"""runspec-playbook — run bundled Ansible playbooks locally, driven by the console.

Public helpers are re-exported so a private corporate wrapper can import the
pure logic (and bake in its own index/venv conventions) without going through
the CLI:

    from runspec_playbook import compose_package_spec, run_ansible
"""

from runspec_playbook.run_playbook import (
    compose_package_spec,
    main_run_playbook,
    parse_extra_vars,
    run_ansible,
    summarize_stats,
)

__all__ = [
    "compose_package_spec",
    "main_run_playbook",
    "parse_extra_vars",
    "run_ansible",
    "summarize_stats",
]
