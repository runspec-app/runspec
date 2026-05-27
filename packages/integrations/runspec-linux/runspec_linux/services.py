import json
import shutil
import subprocess
import sys

import runspec as rs


def _systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def main_check_service() -> None:
    spec = rs.parse("check-service")
    service = str(spec.service)

    if not _systemctl_available():
        print(json.dumps({"error": "systemctl not available — not a systemd system"}))
        sys.exit(1)

    try:
        result = subprocess.run(
            [
                "systemctl",
                "show",
                service,
                "--property=ActiveState,SubState,LoadState,Description,ActiveEnterTimestamp",
            ],
            capture_output=True,
            text=True,
        )
        props: dict[str, str] = {}
        for line in result.stdout.strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v

        active = props.get("ActiveState", "unknown")
        print(
            json.dumps(
                {
                    "service": service,
                    "active": active == "active",
                    "active_state": active,
                    "sub_state": props.get("SubState", "unknown"),
                    "load_state": props.get("LoadState", "unknown"),
                    "description": props.get("Description", ""),
                    "since": props.get("ActiveEnterTimestamp", ""),
                }
            )
        )
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_list_services() -> None:
    spec = rs.parse("list-services")
    state_filter = str(spec.state)

    if not _systemctl_available():
        print(json.dumps({"error": "systemctl not available — not a systemd system"}))
        sys.exit(1)

    try:
        cmd = ["systemctl", "list-units", "--type=service", "--no-pager", "--no-legend"]
        if state_filter != "all":
            cmd += [f"--state={state_filter}"]

        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        rows = []
        for line in result.stdout.strip().splitlines():
            parts = line.split(None, 4)
            if len(parts) < 4:
                continue
            rows.append(
                {
                    "unit": parts[0],
                    "load": parts[1],
                    "active": parts[2],
                    "sub": parts[3],
                    "description": parts[4] if len(parts) > 4 else "",
                }
            )
        print(json.dumps(rows))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_restart_service() -> None:
    spec = rs.parse("restart-service")
    service = str(spec.service)

    if not _systemctl_available():
        print(json.dumps({"error": "systemctl not available — not a systemd system"}))
        sys.exit(1)

    try:
        result = subprocess.run(
            ["systemctl", "restart", service],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(
                json.dumps(
                    {
                        "service": service,
                        "restarted": False,
                        "error": result.stderr.strip(),
                    }
                )
            )
            sys.exit(1)
        print(json.dumps({"service": service, "restarted": True}))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
