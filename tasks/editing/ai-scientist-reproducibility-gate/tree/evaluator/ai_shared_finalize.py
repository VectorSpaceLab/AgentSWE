"""AI Scientist evidence adapters for the centrally owned formal publisher."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SHARED = Path("@@AGENTSWE_EDITING_CONTROL@@/formal_axes_shared.py")


def load_shared(path: Path = SHARED):
    spec = importlib.util.spec_from_file_location("agentswe_ai_formal_axes", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.ROOT = ROOT
    # module.main.__globals__ is the live module dictionary. runpy's returned
    # dictionary is a copy and cannot safely install task-specific callbacks.
    return module


def configure(module, run: Path):
    from case_evidence import prepare_case_evidence
    from semantic_finalize import artifact_for, inside, sha256_file, tree_digest

    original_freeze = module.freeze_document
    original_lifecycle = module.hidden_lifecycle_errors
    cached = {}

    def freeze_document(run_dir):
        path, freeze = original_freeze(run_dir)
        freeze = dict(freeze)
        freeze["candidate_path"] = freeze.get("frozen_candidate_path") or freeze.get("candidate_path")
        freeze["candidate_digest"] = freeze.get("candidate_materialized_digest") or freeze.get("candidate_digest")
        return path, freeze

    def hidden_lifecycle_errors(hidden, freeze, run_dir, selected):
        errors = original_lifecycle(hidden, freeze, run_dir, selected)
        freeze_path, _ = original_freeze(run_dir)
        if freeze_path is not None and hidden.get("freeze_manifest_sha256") != sha256_file(freeze_path):
            errors.append("AI Scientist hidden evidence is not bound to immutable freeze manifest")
        digest = freeze.get("candidate_materialized_digest") or freeze.get("candidate_digest")
        candidate = Path(str(freeze.get("frozen_candidate_path") or freeze.get("candidate_path") or ""))
        if not candidate.is_dir() or not inside(candidate, run_dir) or tree_digest(candidate) != digest:
            errors.append("AI Scientist frozen materialized product tree is missing or changed")
        if hidden.get("frozen_candidate_digest") != digest:
            errors.append("AI Scientist hidden frozen Candidate digest mismatch")
        for entry in hidden.get("cases", []):
            if entry.get("case_id") not in selected:
                continue
            if entry.get("frozen_digest_before") != digest or entry.get("frozen_digest_after") != digest:
                errors.append(f"{entry.get('case_id')}: Candidate changed across lower execution")
            started, finished = entry.get("started_at"), entry.get("finished_at")
            if not started or not finished or started < str(freeze.get("frozen_at", "")) or finished < started:
                errors.append(f"{entry.get('case_id')}: invalid post-freeze phase timing")
        return errors

    def find_case_record(hidden, case_id):
        # The shared generic finder recurses through its replaceable global.
        # Calling it after installing this adapter can project an entry twice.
        # AI's canonical attestation has one direct list entry per case.
        entries = hidden.get('cases') if isinstance(hidden, dict) else None
        matches = [entry for entry in entries if isinstance(entry, dict)
                   and entry.get('case_id') == case_id] if isinstance(entries, list) else []
        if len(matches) != 1:
            return None
        entry = matches[0]
        try:
            path = Path(str(entry["result_path"])).resolve()
            if not inside(path, run) or not path.is_file() or sha256_file(path) != entry.get("result_sha256"):
                raise ValueError("launcher result does not match case-local attestation")
            _freeze_path, freeze = freeze_document(run)
            evidence = prepare_case_evidence(
                case_id=case_id, record_path=path,
                candidate=Path(str(freeze["candidate_path"])),
                candidate_digest=freeze["candidate_digest"],
                output=run / "formal_scoring/result_axis" / case_id / "inputs",
            )
            record = evidence["execution_record"]
            record["_evaluator_record_path"] = str(path)
            cached[case_id] = evidence
            return record
        except Exception as exc:
            # Missing/tampered evidence is unresolved, never a free Candidate0.
            return {"case_id": case_id, "classification": "evidence_unresolved",
                    "evidence_preparation_error": f"{type(exc).__name__}: {exc}"}

    def case_files(run_dir, case_id, record, hidden_path):
        if case_id not in cached:
            raise ValueError(record.get("evidence_preparation_error", "case evidence was not prepared"))
        evidence = cached[case_id]
        return {"artifact": evidence["artifact"], "trajectory": evidence["raw_trajectory"],
                "native": evidence["native_evidence"], "case_input": evidence["case_input"],
                "rubric": evidence["rubric"], "score_cap_contract": evidence.get("score_cap_contract")}

    def validate_model_artifact_provenance(record, artifact, trajectory, case_id):
        try:
            verified = artifact_for("ai_scientist", run, case_id,
                                    Path(record["_evaluator_record_path"]), record, root=ROOT)
            if verified != Path(artifact).resolve():
                return ["AI Scientist artifact path changed after evidence validation"]
            return []
        except Exception as exc:
            return [f"AI Scientist typed provenance: {type(exc).__name__}: {exc}"]

    def provenance_summary(case_id, record, artifact, destination):
        # The private input contains scientific expected-vs-observed facts and
        # case-world comparisons, not only nonce/hash/protocol assertions.
        return cached[case_id]["private_oracle"]

    module.freeze_document = freeze_document
    module.hidden_lifecycle_errors = hidden_lifecycle_errors
    module.find_case_record = find_case_record
    module.case_files = case_files
    module.validate_model_artifact_provenance = validate_model_artifact_provenance
    module.provenance_summary = provenance_summary
    return module


def translate_arguments(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--layout", choices=["ai_scientist"])
    parser.add_argument("--code-rubric")
    parser.add_argument("--judge-timeout")
    _legacy, arguments = parser.parse_known_args(argv)
    return ["--result-judge-broker-endpoint" if value == "--result-broker-endpoint" else value
            for value in arguments]


def finalize(argv=None):
    arguments = translate_arguments(list(sys.argv[1:] if argv is None else argv))
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--run-dir", type=Path, required=True)
    args, _unknown = parser.parse_known_args(arguments)
    module = configure(load_shared(), args.run_dir.resolve())
    return module.main(arguments)
