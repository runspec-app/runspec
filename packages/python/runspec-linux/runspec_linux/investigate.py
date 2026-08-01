"""Wrappers for the application discovery & investigation runnables."""

import json
import sys

import runspec as rs
from runspec_linux_core import failed_units, open_ports, owning_package, service_detail

from runspec_linux._escalation import resolve_escalation


def main_service_detail() -> None:
    spec = rs.parse("service-detail")
    unit = spec.unit.value
    run_as, method = resolve_escalation(spec)
    try:
        print(json.dumps(service_detail(unit, run_as=run_as, become_method=method)))
    except Exception as e:
        print(json.dumps({"error": str(e), "unit": unit}))
        sys.exit(1)


def main_open_ports() -> None:
    spec = rs.parse("open-ports")
    proto = spec.proto.value
    run_as, method = resolve_escalation(spec)
    try:
        print(json.dumps(open_ports(proto, run_as=run_as, become_method=method)))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_owning_package() -> None:
    spec = rs.parse("owning-package")
    path = str(spec.path.value)
    try:
        print(json.dumps(owning_package(path)))
    except Exception as e:
        print(json.dumps({"error": str(e), "path": path}))
        sys.exit(1)


def main_failed_units() -> None:
    rs.parse("failed-units")
    try:
        print(json.dumps(failed_units()))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
