from __future__ import annotations
import hashlib,json,os,signal,subprocess,sys,time
from pathlib import Path
from agentloop.protocol import write_json,read_json,BUILDER_MODEL,BUILDER_EFFORT
DIRECT_BUILDER_SCRIPT=Path("@@AGENTSWE_EDITING_CONTROL@@/direct_harbor_builder.py")
def direct_builder_runtime():
    if str(DIRECT_BUILDER_SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(DIRECT_BUILDER_SCRIPT.parent))
    import direct_harbor_builder
    return direct_harbor_builder

SHARED_BUILDER_SEGMENTS = "@@AGENTSWE_EDITING_CONTROL@@"


def builder_segments_runtime():
    """The shared Builder segment loop (package 97, builder_segments.py).

    One codex session may span more than one Harbor trial when an evaluator-side
    infrastructure cut ends a container mid-turn; that module owns the whole
    decision, the relaunch and <run>/builder_segments.json, so that all ten
    trees behave identically.  A run that never resumes is unaffected.
    """
    import sys as _sys
    if SHARED_BUILDER_SEGMENTS not in _sys.path:
        _sys.path.insert(0, SHARED_BUILDER_SEGMENTS)
    import builder_segments
    return builder_segments


def _run_native_builder(*, lifecycle, config: Path, credential: Path,
                       harbor: Path, timeout: int = 28800,
                       proxy: str = "http://127.0.0.1:7890"):
    """Actual consumer for user-authorized native direct Builder authentication.

    Lower and Judge remain evaluator-owned. Never reconstruct or retry a
    Builder request here; the unmodified native CLI owns its stream recovery.
    """
    direct = direct_builder_runtime()
    run = lifecycle.run_dir
    composition = run / "builder_task/environment/docker-compose.yaml"
    compose = read_json(composition)
    compose["services"]["main"].get("environment", {}).pop("AGENTSWE_BUILDER_BROKER_TOKEN", None)
    write_json(composition, compose)
    child = None
    from harbor.builder_resources import BuilderResourceObserver
    resource_observer = BuilderResourceObserver(run)
    resource_observer.start()
    try:
        with direct.existing_proxy_for_builder(config, composition, proxy) as proxy_receipt:
            write_json(run / "builder_proxy.json", proxy_receipt)
            with direct.direct_auth(config, credential) as auth_receipt:
                write_json(run / "builder_transport.json", {**auth_receipt,
                    "shared_runtime_sha256": hashlib.sha256(DIRECT_BUILDER_SCRIPT.read_bytes()).hexdigest(),
                    "native_model": BUILDER_MODEL, "native_effort": BUILDER_EFFORT,
                    "outer_timeout_seconds": timeout})
                # 97: one codex session, up to RESUME_CAP + 1 Harbor jobs.
                # See builder_segments_runtime(); the launch loop itself is
                # unchanged and now lives in that shared module.
                def adopt_builder_child(process):
                    nonlocal child
                    child = process
                with builder_segments_runtime().builder_session(
                        run, config, harbor=harbor, observer=resource_observer,
                        write_json=write_json, on_start=adopt_builder_child,
                        env={**os.environ, "PYTHONPATH": str(DIRECT_BUILDER_SCRIPT.parent)}) as session:
                    code = session.run(budget_seconds=timeout)
                    resources = resource_observer.finish()
                    if not resources["valid"]: code = 125
                    gate_paths = list((run / "jobs").rglob("builder_resource_gate.json"))
                    gate_proof = {"valid": False, "files": [], "errors": []}
                    try:
                        if len(gate_paths) != 1:
                            raise ValueError("expected exactly one pre-agent resource/runtime gate")
                        gate_path = gate_paths[0]
                        if any(p.is_symlink() for p in (gate_path, *gate_path.parents)):
                            raise ValueError("pre-agent gate contains a symlink")
                        gate_data = gate_path.read_bytes()
                        gate = json.loads(gate_data)
                        gate_proof["files"].append({"path": str(gate_path),
                            "sha256": hashlib.sha256(gate_data).hexdigest(), "gate": gate})
                        trial_path = gate_path.parent.parent / "result.json"
                        if trial_path.is_symlink():
                            raise ValueError("Harbor trial result is a symlink")
                        trial_data = trial_path.read_bytes()
                        trial = json.loads(trial_data)
                        gate_proof["harbor_trial"] = {"path": str(trial_path),
                            "sha256": hashlib.sha256(trial_data).hexdigest(),
                            **{key: trial.get(key) for key in ("exception_info", "agent_setup", "agent_execution")}}
                        if (trial.get("exception_info") or {}).get("exception_type") == "HealthcheckError":
                            raise ValueError("Harbor reported HealthcheckError before Agent setup")
                        if gate.get("valid") is not True or gate.get("runtime", {}).get("valid") is not True:
                            raise ValueError("pre-agent resource/runtime gate rejected the environment")
                        gate_proof["valid"] = True
                    except (OSError, ValueError, TypeError) as exc:
                        gate_proof["errors"].append(str(exc))
                    write_json(run / "builder_preagent_gate_attestation.json", gate_proof)
                    if not gate_proof["valid"]: code = 125
                    return subprocess.CompletedProcess(child.args, code)
    finally:
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try: child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL); child.wait()
        resource_observer.finish()
        cleanup = resource_observer.cleanup_owned(process_started=child is not None,
            defer_removal=bool(getattr(lifecycle, 'readiness_profile', None)))
        write_json(run / "builder_native_stats.json", direct.native_stats(run))
        if not cleanup["complete"] and not cleanup.get('retained_terminal'):
            raise RuntimeError("native Builder container cleanup or terminal retention is unproven")


def run_native_builder(**kwargs):
    """Give evaluator build callbacks the already validated offline snapshot."""
    lifecycle = kwargs['lifecycle']
    modules = getattr(lifecycle, 'builder_node_modules', None)
    if modules is None or not Path(modules).is_dir():
        raise RuntimeError('Builder dependency preflight was not completed')
    previous = os.environ.get('OPENWIKI_NODE_MODULES')
    os.environ['OPENWIKI_NODE_MODULES'] = str(modules)
    try:
        return _run_native_builder(**kwargs)
    finally:
        if previous is None:
            os.environ.pop('OPENWIKI_NODE_MODULES', None)
        else:
            os.environ['OPENWIKI_NODE_MODULES'] = previous
