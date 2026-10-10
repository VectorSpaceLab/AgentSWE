"""OpenHands task-native adapters for the centrally owned formal publisher."""
from __future__ import annotations
import argparse
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SHARED = Path("@@AGENTSWE_EDITING_CONTROL@@/formal_axes_shared.py")


def finalize(argv=None):
    from case_evidence import prepare_case_evidence, read, sha, tree_digest
    arguments = list(sys.argv[1:] if argv is None else argv)
    legacy = argparse.ArgumentParser(add_help=False)
    legacy.add_argument("--layout", choices=["openhands"])
    legacy.add_argument("--code-rubric")
    legacy.add_argument("--judge-timeout")
    _known, arguments = legacy.parse_known_args(arguments)
    arguments = ["--result-judge-broker-endpoint" if arg == "--result-broker-endpoint" else arg for arg in arguments]
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--run-dir", type=Path, required=True)
    args, _unknown = parser.parse_known_args(arguments)
    run = args.run_dir.resolve()
    spec = importlib.util.spec_from_file_location("agentswe_openhands_formal_axes", SHARED)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.ROOT = ROOT
    original_find = module.find_case_record
    original_lifecycle = module.hidden_lifecycle_errors
    cached = {}

    def find_case_record(hidden, case_id):
        entry = original_find(hidden, case_id)
        if entry is None: return None
        # 2026-09-20 (formal 0920-fh-002 test_001): cached evidence wins over a repeated
        # preparation -- the second lookup of a case re-ran prepare_case_evidence into the
        # inputs directory the first one created, the FileExistsError was swallowed below
        # and an unbound stub refused the whole run.
        # fcr-reentrancy-guard: original_find recurses through this module's own global
        # name, which this adapter has replaced, so a nested lookup re-enters here
        # and returns the execution record an inner call already built. Adapting it
        # again raises KeyError on the absent result_path and throws away a record
        # that is correctly bound to the frozen Candidate.
        # 2026-09-21 (formal 0921b-v4-001 test_004, package 101 on 167): the guard also has to
        # cover the record of a case that produced NO artifact. A case the evaluator stopped at
        # the case-budget reserve, and the D23 malformed-artifact early return, both
        # carry a bound execution record with artifact_path absent; requiring
        # artifact_path here sent them back into the result_path branch, where the
        # KeyError became an "evidence_unresolved" stub and the publisher refused the
        # whole run with "execution is not bound to the frozen Candidate digest".
        # Once a case is cached, the inner call's adapted record is authoritative
        # whatever shape the re-entered entry has.
        # package 118 (27): a timed-out case has no artifact_path, yet its record is
        # just as built and just as bound; identify a built record by identity.
        # OE canonical merge (2026-10-01): 101's cached early return + 118's identity test
        # (118's condition is the weaker of the two, so it subsumes 101's second test).
        if case_id in cached and isinstance(entry, dict):
            return cached[case_id]["execution_record"]
        if (isinstance(entry, dict) and "result_path" not in entry
                and entry.get("candidate_digest") and entry.get("case_id") == case_id):
            return entry
        try:
            path = Path(entry["result_path"]).resolve()
            if not module.local(path, run) or not path.is_file() or sha(path) != entry.get("result_sha256"):
                raise ValueError("hidden case record differs from run-local attestation")
            _freeze_path, freeze = module.freeze_document(run)
            candidate = Path(freeze["frozen_candidate_path"])
            evidence = prepare_case_evidence(case_id=case_id, record_path=path, candidate=candidate,
                                            candidate_digest=freeze["candidate_materialized_digest"],
                                            output=run / "formal_scoring/result_axis" / case_id / "inputs")
            cached[case_id] = evidence
            return evidence["execution_record"]
        except Exception as exc:
            return {"case_id": case_id, "classification": "evidence_unresolved", "evidence_preparation_error": f"{type(exc).__name__}: {exc}"}

    def case_files(run_dir, case_id, record, hidden_path):
        if case_id not in cached: raise ValueError(record.get("evidence_preparation_error", "case evidence missing"))
        evidence = cached[case_id]
        return {"artifact": evidence["artifact"], "trajectory": evidence["raw_trajectory"],
                "native": evidence["native_evidence"], "case_input": evidence["case_input"], "rubric": evidence["rubric"]}

    def validate_model_artifact_provenance(record, artifact, trajectory, case_id):
        evidence = cached.get(case_id)
        if evidence is None: return ["typed OpenHands provenance was not checked"]
        validation = evidence["execution_record"]["artifact_validation"]
        return [] if validation.get("valid") is True and validation.get("sha256") == sha(artifact) else ["OpenHands artifact changed after typed validation"]

    def hidden_lifecycle_errors(hidden, freeze, run_dir, selected):
        # Older same-task attestation uses a relative freeze filename. Resolve
        # only this known field under its actual lifecycle directory.
        hidden = dict(hidden)
        freeze_path, _ = module.freeze_document(run_dir)
        hidden["freeze_manifest"] = str(freeze_path) if freeze_path else None
        errors = original_lifecycle(hidden, freeze, run_dir, selected)
        if freeze_path is None or hidden.get("freeze_manifest_sha256") != sha(freeze_path): errors.append("OpenHands freeze binding mismatch")
        candidate = Path(freeze.get("frozen_candidate_path", ""))
        if not candidate.is_dir() or not module.local(candidate, run_dir) or tree_digest(candidate) != freeze.get("candidate_materialized_digest"):
            errors.append("OpenHands frozen product source changed")
        return errors

    module.find_case_record = find_case_record
    module.case_files = case_files
    module.validate_model_artifact_provenance = validate_model_artifact_provenance
    module.provenance_summary = lambda case_id, record, artifact, destination: cached[case_id]["private_oracle"]
    module.hidden_lifecycle_errors = hidden_lifecycle_errors
    return module.main(arguments)
