import json
import sys

import runspec as rs
from runspec_linux_core import block_devices, check_memory, disk_usage, list_processes, system_info


def main_system_info() -> None:
    rs.parse("system-info")
    try:
        print(json.dumps(system_info()))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_disk_usage() -> None:
    rs.parse("disk-usage")
    try:
        print(json.dumps(disk_usage()))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_block_devices() -> None:
    spec = rs.parse("block-devices")
    include_loop = spec.all.value
    try:
        print(json.dumps(block_devices(include_loop=include_loop)))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_check_memory() -> None:
    rs.parse("check-memory")
    try:
        print(json.dumps(check_memory()))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_list_processes() -> None:
    spec = rs.parse("list-processes")
    name_filter = spec.filter.value
    limit = spec.limit.value
    try:
        print(json.dumps(list_processes(name_filter, limit)))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
