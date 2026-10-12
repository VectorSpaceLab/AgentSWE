"""Provider-free filesystem/dependency probe of the actual lower sandbox."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agentloop.lower_agent_entry import _sandbox_command


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    runtime = args.output / "runtime"
    runtime.mkdir()
    paths = [str(ROOT / "agentloop/case_fixture.py"), str(ROOT / "evaluator/cases/test_002.json"),
        "@@AGENTSWE_CREDENTIAL_FILE@@",
        "@@AGENTSWE_EDITING_CONTROL@@/result_judge.py"]
    probe = "import socket; socket.create_connection=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('network disabled')); "
    probe += "import json,pathlib; import deeptutor.learning.models; "
    probe += "paths=" + repr(paths) + "; print(json.dumps({'product_dependency_imported':True,'private_paths_visible':{p:pathlib.Path(p).exists() for p in paths}}))"
    command, mounts = _sandbox_command([args.python, "-c", probe], args.repository, runtime, args.python)
    completed = subprocess.run(command, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(args.repository), "HOME": str(runtime)},
        capture_output=True, text=True, timeout=30)
    result = {"exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr,
              "mounts": mounts, "provider_calls": 0}
    if completed.returncode == 0:
        parsed = json.loads(completed.stdout)
        result["passed"] = parsed["product_dependency_imported"] and not any(parsed["private_paths_visible"].values())
    else:
        result["passed"] = False
    (args.output / "verification.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    return 0 if result["passed"] else 2

if __name__ == "__main__":
    raise SystemExit(main())
