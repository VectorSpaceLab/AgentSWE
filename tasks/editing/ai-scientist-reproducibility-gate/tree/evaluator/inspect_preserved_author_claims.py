"""Read-only reclassification diagnostic; never rescoring an old acceptance."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agentloop"))
from lower_agent_launcher import validate_authored_artifact
from protocol import write_json


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path): return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = args.case_output.resolve()
    record_path = base / "launcher_result.json"
    record = read(record_path)
    artifact = base / "agent_result.rejected.json"
    captured = record.get("rejected_agent_artifact_sha256")
    if not artifact.is_file():
        artifact = base / "agent_result.json"
        captured = record.get("agent_artifact_sha256")
    if sha(artifact) != captured:
        raise ValueError("preserved author artifact changed")
    raw_path = base / "raw_action_trajectory.json"
    if sha(raw_path) != record["action_loop"]["raw_trajectory_sha256"]:
        raise ValueError("preserved action evidence changed")
    integrity = record["integrity"]
    for name, digest in integrity["artifact_hashes"].items():
        if sha(base / name) != digest:
            raise ValueError("preserved product file changed")
    mismatches = validate_authored_artifact(read(artifact), case_id=record["case_id"],
        rollout_digest=integrity["rollout_digest"], hashes=integrity["artifact_hashes"],
        trajectory=read(raw_path)["events"], trajectory_digest=integrity["trajectory_digest"],
        runtime_digest=integrity["runtime_case_digest"], case_world_digest=integrity["case_world_digest"])
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(args.output / "diagnostic.json", {
        "schema_version": "agentswe-preserved-author-claim-diagnostic/v1",
        "source_record": str(record_path), "source_record_sha256": sha(record_path),
        "source_artifact": str(artifact), "source_artifact_sha256": sha(artifact),
        "source_files_modified": False, "model_calls": 0,
        "case_rollout_primary_artifact_and_persisted_capture_checks": "passed",
        "full_new_raw_response_origin_check": "unavailable on historical run; no fabricated backfill",
        "semantic_claim_mismatches": mismatches,
        "result_score": None, "formal_result_publishable": False,
        "acceptance_result_publishable": False,
        "historical_disposition": "contract-ambiguity; not Candidate fault solely for block plus audit commit",
    })
    print(json.dumps({"diagnostic": str(args.output / "diagnostic.json"), "mismatches": [item["field"] for item in mismatches], "source_unchanged": sha(artifact) == captured}))


if __name__ == "__main__": main()
