"""No-provider scope, memory-pressure, deadline and ownership negative tests."""
import argparse
import json
from pathlib import Path
import sys
import subprocess
import time
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agentloop.owned_resources import MEMORY_BYTES, run_owned, verify_identity, stop_owned


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    options.output.mkdir(parents=True, exist_ok=False)
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    normal, normal_record = run_owned([sys.executable, "-I", "-c", "print('actual-owned-case')"],
        cwd="/", env=env, output=options.output / "normal", timeout=5)
    pressure, pressure_record = run_owned([sys.executable, "-I", "-c", "x=bytearray(256*1024*1024);print(len(x))"],
        cwd="/", env=env, output=options.output / "pressure", timeout=10, memory_bytes=64 * 1024 * 1024)
    pid_path = options.output / "detached-child.pid"
    code = "import subprocess,pathlib,time; p=subprocess.Popen(['/usr/bin/sleep','30'], start_new_session=True);pathlib.Path(" + repr(str(pid_path)) + ").write_text(str(p.pid));time.sleep(30)"
    timeout, timeout_record = run_owned([sys.executable, "-I", "-c", code], cwd="/", env=env,
        output=options.output / "timeout", timeout=1.5)
    child_pid = int(pid_path.read_text())
    stat = Path(f"/proc/{child_pid}/stat")
    child_running = stat.exists() and stat.read_text().rsplit(")", 1)[1].strip().split()[0] != "Z"
    ownership_refused = False
    bad = dict(normal_record["systemd_properties"], Description="another unrelated owner")
    try:
        verify_identity(bad, normal_record)
    except RuntimeError:
        ownership_refused = True
    abandoned_output = options.output / "controller-killed"
    hostile_command = [sys.executable, "-I", "-c", "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)"]
    child_script = "import sys;from pathlib import Path;sys.path.insert(0," + repr(str(ROOT)) + ");from agentloop.owned_resources import run_owned;run_owned(" + repr(hostile_command) + ",cwd='/',env={'PATH':'/usr/bin:/bin'},output=Path(" + repr(str(abandoned_output)) + "),timeout=3)"
    controller = subprocess.Popen([sys.executable, "-I", "-c", child_script], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started = time.monotonic()
    while not (abandoned_output / "scope-permit").exists() and time.monotonic() - started < 5 and controller.poll() is None:
        time.sleep(.02)
    controller.kill()
    controller.wait(timeout=3)
    expected_abandoned = json.loads((abandoned_output / "scope-ownership.json").read_text())
    scope_root = Path("/sys/fs/cgroup") / expected_abandoned["cgroup"].lstrip("/")
    while scope_root.exists() and time.monotonic() - started < 9:
        if "populated 0" in (scope_root / "cgroup.events").read_text():
            break
        time.sleep(.05)
    independent_empty = not scope_root.exists() or "populated 0" in (scope_root / "cgroup.events").read_text()
    # Still scoped ownership-checked cleanup if the independent guarantee failed.
    abandoned_cleanup = stop_owned(expected_abandoned)
    checks = {"normal_actual_process_4GiB_limit": normal.returncode == 0 and normal.stdout.strip() == "actual-owned-case" and normal_record["observed"]["memory_max"] == str(MEMORY_BYTES),
        "memory_pressure_child_cannot_exceed_smaller_test_cap": pressure.returncode != 0 and pressure_record["observed"]["memory_max"] == str(64 * 1024 * 1024),
        "deadline_is_not_per_stage_reset": timeout.returncode == 124 and timeout_record["timed_out"] and timeout_record["elapsed_seconds"] < 12,
        "detached_child_not_running_after_timeout": not child_running,
        "all_owned_subtrees_empty_after_return": all(r["cleanup"]["complete"] for r in (normal_record, pressure_record, timeout_record)),
        "wrong_owner_identity_refused_before_control": ownership_refused}
    checks["SIGKILL_controller_still_leaves_no_running_subtree"] = independent_empty
    result = {"all_passed": all(checks.values()), "checks": checks, "provider_calls": 0,
        "memory_pressure_probe_configuration_delta": {"test_only_memory_mib": 64, "formal_memory_mib": 4096},
        "normal": normal_record, "pressure": pressure_record, "timeout": timeout_record}
    result.update(controller_killed_cleanup=abandoned_cleanup, pressure_process_exit_code=pressure.returncode,
        pressure_stderr=pressure.stderr, pressure_failure_cause="not inferred without memory.events")
    (options.output / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("all_passed", "checks", "provider_calls")}))
    return 0 if result["all_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
