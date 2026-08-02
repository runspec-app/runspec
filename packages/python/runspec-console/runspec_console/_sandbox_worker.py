"""
_sandbox_worker.py — the standalone subprocess that actually runs guest code in
the WASM sandbox (see ``sandbox.py``).

Invoked as ``python <this-file>`` with a one-line JSON job on stdin; it
deserializes the pre-compiled WASI CPython module, runs it under WASI with the
preopens/argv from the job, and prints a one-line JSON result on stdout. Guest
stdout/stderr go to the files named in the job.

Run as a **subprocess on purpose**: an in-process wasmtime call can't be killed
while the guest is blocked in a host syscall (`time.sleep`, a blocking read), so
neither a wall-clock timeout nor the operator's Cancel could interrupt it. A
child process can always be killed by the parent, giving a hard guarantee.

Kept import-light — wasmtime + stdlib only, **no** ``runspec_console`` import — so
it's launched by file path (not ``-m``), skipping package init for fast startup.
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    job = json.loads(sys.stdin.read() or "{}")
    import wasmtime as w  # noqa: PLC0415 # type: ignore[import-not-found]

    cfg = w.Config()
    cfg.consume_fuel = True  # MUST match the config used to serialize the module
    engine = w.Engine(cfg)
    with open(job["cwasm"], "rb") as f:
        blob = f.read()
    module = w.Module.deserialize(engine, blob)

    store = w.Store(engine)
    store.set_fuel(int(job["fuel"]))
    store.set_limits(memory_size=int(job["mem_bytes"]))

    wasi = w.WasiConfig()
    wasi.argv = list(job["argv"])
    wasi.preopen_dir(job["root"], "/ws")
    wasi.preopen_dir(job["exec_dir"], "/exec")
    wasi.stdout_file = job["stdout"]
    wasi.stderr_file = job["stderr"]
    store.set_wasi(wasi)

    linker = w.Linker(engine)
    linker.define_wasi()

    result = {"exit_code": 0, "trap": ""}
    try:
        instance = linker.instantiate(store, module)
        instance.exports(store)["_start"](store)
    except w.ExitTrap as e:  # normal sys.exit path (0 on success)
        result["exit_code"] = int(getattr(e, "code", 0) or 0)
    except w.Trap as e:  # fuel exhaustion or a genuine wasm trap
        result["exit_code"] = 1
        result["trap"] = (str(e).splitlines()[0][:200]) if str(e) else "trap"
    except Exception as e:  # noqa: BLE001 — report, don't crash the worker silently
        result["exit_code"] = 1
        result["trap"] = f"{type(e).__name__}: {e}"[:200]

    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
