"""runspec-linux-root — Linux admin runnables that need root.

Package management, reboot and kernel tuning, split out of ``runspec-linux`` so
that package never runs anything as root. Every tool here declares
``run_as = "root"``. Install it into a **root-owned** venv: root runs this code,
so only root may be able to change it. The logic lives in ``runspec-linux-core``.
"""
