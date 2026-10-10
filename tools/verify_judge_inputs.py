#!/usr/bin/env python3
"""Replay archived Creation judge inputs through run_eval.py and check that the first judge request is unchanged.

    python3 tools/verify_judge_inputs.py --task repository-bug-repair --run RUN_DIR [--run ...] --scratch DIR \\
        [--ledger judge-events.jsonl ...] [--reference staged|PATH] [--compare body|prompt]
        [--overflow-case PHASE/CASE] [--dump-prompts]

For every staged eval task under RUN_DIR/evaluations/*/eval_tasks/<case>, or RUN_DIR/eval_tasks/<case> (an eval-only
run), of a Creation task whose eval carries the overflow fallback (repository-bug-repair, database-analytics,
formal-theorem-proving, schema-guided-web-extraction, web-research-report) it rebuilds the judge request offline: the
archived manifest, active case, eval prompt, rubric and candidate output (read from the task's compose binds) go
through run_eval.py main() with the judge endpoint faked, and the request body it would send is captured. Nothing is
sent and the archive is only read; outputs go under --scratch.

  * --ledger: the request body's sha256 (json.dumps(body, sort_keys=True, ensure_ascii=False), as the judge broker
    hashes it) must be in the ledger's body_sha256 set (release runs, whose judge went through the broker).
  * --reference staged: the archived copy of run_eval.py that ran for the case (or PATH) builds the request from the
    same inputs; the two request bodies must be byte-identical (paper archives, whose judge was called directly).
    With --compare prompt only the judge input must be byte-identical: the prompt text and every image part, for an
    archive whose run_eval.py sent them to a judge configured differently (model, effort, output limit, streaming).
  * the replayed harness evidence must equal the archived eval_result.json harness_result when that file exists.
  * --overflow-case: replays one case against a judge that answers the first request with the provider's
    context-overflow HTTP 400 and the second with a judge result, and prints the prompt_reduction receipt.

Exit status 0 only when every first request matched. Credentials are never read: the credential bind is replaced by a
missing file and the judge key by a placeholder.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TASK_FILES = {task: f"tasks/creation/{task}/adapter/eval-template/solution/run_eval.py" for task in (
    "repository-bug-repair", "database-analytics", "formal-theorem-proving", "schema-guided-web-extraction",
    "web-research-report")}
ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER_KEY = "replay-placeholder-not-a-key"
OVERFLOW_BODY = ('{"error":{"message":"This model\'s maximum context length is 1048576 tokens. However, you requested '
                 '{total} tokens ({messages} in the messages, 100000 in the completion). Please reduce the length of '
                 'the messages or completion.","type":"invalid_request_error","param":null,'
                 '"code":"invalid_request_error"}}')

CAPTURE = r'''
import contextlib, copy, hashlib, importlib.util, io, json, os, pathlib, sys
spec = json.loads(sys.argv[1])
real_stdout = sys.stdout
module_spec = importlib.util.spec_from_file_location("run_eval_replay", spec["run_eval"])
module = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(module)
requests_sent = []
responses = list(spec["responses"])


class Response:
    def __init__(self, status, body=b"", judge=None):
        self.status_code, self.body, self.judge = status, body, judge
        self.headers = {"Content-Type": "text/event-stream" if judge is not None else "application/json"}

    def iter_content(self, chunk_size=8192):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]

    def iter_lines(self, decode_unicode=True):
        yield "data: " + json.dumps({"type": "response.output_text.delta", "delta": json.dumps(self.judge)})
        yield "data: " + json.dumps({"type": "response.completed", "response": {}})

    def json(self):  # a judge called without streaming
        return {"status": "completed", "output_text": json.dumps(self.judge)}

    def close(self):
        pass


def post(url, headers=None, json=None, timeout=None, stream=None):
    import json as _json
    body = _json.dumps(json, sort_keys=True, ensure_ascii=False)
    prompt = json["input"][0]["content"][0]["text"]
    parts = json["input"][0]["content"][1:]
    record = {"broker_sha256": hashlib.sha256(body.encode("utf-8", "surrogatepass")).hexdigest(),
              "prompt_sha256": hashlib.sha256(prompt.encode("utf-8", "surrogatepass")).hexdigest(),
              "images_sha256": hashlib.sha256(_json.dumps(parts, sort_keys=True).encode()).hexdigest(),
              "prompt_chars": len(prompt), "images": len(parts),
              "fields": {k: v for k, v in json.items() if k != "input"}}
    if hasattr(module, "estimate_tokens"):
        record["estimated_tokens"] = module.estimate_tokens(prompt)
        if hasattr(module, "image_token_estimate"):
            record["estimated_image_tokens"] = module.image_token_estimate(parts)
    if spec.get("dump_dir"):
        path = pathlib.Path(spec["dump_dir"]) / ("request_%d.txt" % len(requests_sent))
        path.write_text(prompt, encoding="utf-8", errors="surrogatepass")
        record["prompt_file"] = str(path)
    requests_sent.append(record)
    answer = responses.pop(0) if responses else {"status": 200}
    if answer["status"] == 200:
        return Response(200, judge={"case_id": spec["case_id"], "evaluation_state": "scoreable", "validity_gate": True,
                                    "dimensions": {}, "score": 0, "major_errors": [], "assessment": "replay"})
    return Response(answer["status"], answer.get("body", "").encode())


real_mkdir = pathlib.Path.mkdir


def mkdir(path, *args, **kwargs):
    if not str(path).startswith("/tmp/harbor-"):
        return real_mkdir(path, *args, **kwargs)


pathlib.Path.mkdir = mkdir
module.requests.post = post
module.time.sleep = lambda seconds: None
module.eval_ca_bundle = lambda prefix: pathlib.Path("/nonexistent/replay-ca.pem")
os.environ.update(spec["env"])
sys.argv = ["run_eval.py", "--manifest", spec["manifest"], "--case-root", spec["case_root"], "--candidate-output",
            spec["candidate_output"], "--eval-prompt", spec["eval_prompt"], "--rubric", spec["rubric"], "--harness",
            "/evaluator/evaluate_case.py", "--output-dir", spec["out_dir"], "--credential-file",
            "/nonexistent/replay-credentials.env"]
error = None
with contextlib.redirect_stdout(io.StringIO()):
    try:
        module.main()
    except Exception as exc:
        error = "%s: %s" % (type(exc).__name__, exc)
real_stdout.write(json.dumps({"requests": requests_sent, "error": error}) + "\n")
'''


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def binds(task_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    compose = read_json(task_dir / "environment" / "docker-compose.yaml", {})
    service = (compose.get("services") or {}).get("main") or {}
    volumes = {v.get("target"): v.get("source") for v in service.get("volumes") or [] if isinstance(v, dict)}
    environment = service.get("environment") or {}
    return volumes, {k: str(v) for k, v in environment.items() if k.startswith("AGENTSWE_JUDGE_")
                     and k != "AGENTSWE_JUDGE_API_KEY"} if isinstance(environment, dict) else {}


def archived_eval_result(phase: Path, case: str) -> Path | None:
    config = read_json(phase / "eval_job_config.json", {})
    job = Path(str(config.get("jobs_dir", ""))) / str(config.get("job_name", ""))
    trials = sorted(job.glob(case + "__*")) if job.is_dir() else []
    paths = [t / "artifacts" / "logs" / "artifacts" / "eval" / "eval_result.json" for t in trials]
    paths = [p for p in paths if p.is_file()]
    return paths[-1] if len(paths) == 1 else None


def key_variables(run_eval: Path) -> set[str]:
    """The judge-key environment variables a run_eval.py reads; each is set to the placeholder (an archived
    run_eval.py may read its key under another name)."""
    names = set(re.findall(r"environ\.get\(\s*[\"']([A-Z0-9_]*API_KEY)[\"']", run_eval.read_text(encoding="utf-8")))
    return (names or {"DEEPSEEK_API_KEY"}) - {"AGENTSWE_JUDGE_API_KEY"}


def eval_tasks(run: Path) -> list[Path]:
    """Staged eval tasks of a run: per phase (RUN/evaluations/<phase>/eval_tasks/<case>) or of an eval-only run
    (RUN/eval_tasks/<case>, whose phase is RUN itself)."""
    found = sorted(run.glob("evaluations/*/eval_tasks/*")) + sorted(run.glob("eval_tasks/*"))
    return [task_dir for task_dir in found if (task_dir / "solution" / "eval_manifest.json").is_file()]


def capture(python: str, run_eval: Path, task_dir: Path, case: str, out_dir: Path, responses: list,
            dump_dir: Path | None) -> dict:
    volumes, judge_env = binds(task_dir)
    active = volumes.get(f"/active-case/{case}")
    spec = {"run_eval": str(run_eval), "manifest": str(task_dir / "solution" / "eval_manifest.json"),
            "case_root": str(Path(active).parent) if active else "", "case_id": case,
            "candidate_output": volumes.get("/candidate-output") or str(task_dir / "input" / "candidate_output"),
            "eval_prompt": volumes.get("/evaluator/eval_prompt.md", ""), "rubric": volumes.get("/evaluator/rubric.md", ""),
            "out_dir": str(out_dir), "responses": responses, "dump_dir": str(dump_dir) if dump_dir else None,
            "env": dict(judge_env, **{name: PLACEHOLDER_KEY for name in key_variables(run_eval)})}
    out_dir.mkdir(parents=True, exist_ok=True)
    if dump_dir:
        dump_dir.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not any(t in k.upper() for t in ("KEY", "TOKEN", "SECRET", "AUTH"))}
    env.pop("PYTHONPATH", None)
    completed = subprocess.run([python, "-I", "-B", "-c", CAPTURE, json.dumps(spec)], capture_output=True, text=True,
                               env=env, timeout=600)
    try:
        value = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        value = {"requests": [], "error": "capture failed: " + completed.stderr.strip()[-500:]}
    value["eval_result"] = read_json(out_dir / "eval_result.json")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--task", choices=sorted(TASK_FILES), required=True)
    parser.add_argument("--run", type=Path, action="append", required=True, help="a run directory with evaluations/")
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, action="append", default=[])
    parser.add_argument("--reference", help="'staged' (the archived run_eval.py of each case) or a run_eval.py path")
    parser.add_argument("--compare", choices=("body", "prompt"), default="body",
                        help="with --reference: the whole request body must match (default), or only the judge "
                             "input (prompt text and image parts)")
    parser.add_argument("--run-eval", type=Path, help="the run_eval.py under test (default: this checkout's)")
    parser.add_argument("--python", default=sys.executable, help="interpreter for run_eval.py")
    parser.add_argument("--overflow-case", help="PHASE/CASE to replay against a context-overflow 400")
    parser.add_argument("--dump-prompts", action="store_true", help="keep each request's prompt under --scratch")
    parser.add_argument("--report", type=Path, help="write the per-case report as JSON")
    args = parser.parse_args()
    new_run_eval = (args.run_eval or ROOT / TASK_FILES[args.task]).resolve()
    ledger: dict[str, dict] = {}
    for path in args.ledger:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                event = json.loads(line)
                if event.get("body_sha256"):
                    ledger[event["body_sha256"]] = event
    rows, reference_hashes = [], {}
    for run in args.run:
        for task_dir in eval_tasks(run):
            phase, case = task_dir.parent.parent, task_dir.name
            key = f"{phase.name}/{case}"
            scratch = args.scratch / run.name / phase.name / case
            row: dict = {"case": key, "run": str(run)}
            new = capture(args.python, new_run_eval, task_dir, case, scratch / "new", [],
                          scratch / "prompts" if args.dump_prompts else None)
            row["error"] = new["error"]
            row["requests"] = len(new["requests"])
            first = new["requests"][0] if new["requests"] else None
            if first:
                row.update({k: first.get(k) for k in ("broker_sha256", "prompt_sha256", "images_sha256", "prompt_chars",
                                                      "estimated_tokens", "estimated_image_tokens", "prompt_file",
                                                      "images")})
            archived_path = archived_eval_result(phase, case)
            archived = read_json(archived_path) if archived_path else None
            if archived is not None and isinstance(new["eval_result"], dict):
                row["harness_equal"] = canonical(archived.get("harness_result")) == canonical(
                    new["eval_result"].get("harness_result"))
            if args.ledger and first:
                event = ledger.get(first["broker_sha256"])
                row["ledger_match"] = event is not None
                if event:
                    usage = event.get("usage") or {}
                    row["ledger_status"], row["ledger_input_tokens"] = event.get("status"), usage.get("input_tokens")
            if args.reference:
                reference = (task_dir / "solution" / "run_eval.py") if args.reference == "staged" else Path(
                    args.reference)
                digest = sha256_file(reference)
                reference_hashes[digest] = reference_hashes.get(digest, 0) + 1
                old = capture(args.python, reference.resolve(), task_dir, case, scratch / "reference", [], None)
                row["reference_sha256"] = digest
                row["reference_requests"] = len(old["requests"])
                row["reference_error"] = old["error"]
                same_count = len(old["requests"]) == len(new["requests"])
                pairs = list(zip(old["requests"][:1], new["requests"][:1]))
                row["reference_body_match"] = same_count and all(
                    a["broker_sha256"] == b["broker_sha256"] for a, b in pairs)
                row["reference_prompt_match"] = same_count and all(
                    (a["prompt_sha256"], a["images_sha256"]) == (b["prompt_sha256"], b["images_sha256"])
                    for a, b in pairs)
                row["reference_match"] = row["reference_body_match" if args.compare == "body" else
                                             "reference_prompt_match"]
                if pairs and not row["reference_body_match"]:
                    row["reference_field_differences"] = sorted(
                        key for key in set(pairs[0][0]["fields"]) | set(pairs[0][1]["fields"])
                        if pairs[0][0]["fields"].get(key) != pairs[0][1]["fields"].get(key))
            if args.overflow_case == key and first:
                tokens = row.get("ledger_input_tokens")
                messages = int(tokens) - 30 if isinstance(tokens, int) else 1_360_700
                body = OVERFLOW_BODY.replace("{total}", str(messages + 100000)).replace("{messages}", str(messages))
                replay = capture(args.python, new_run_eval, task_dir, case, scratch / "overflow",
                                 [{"status": 400, "body": body}, {"status": 200}], scratch / "overflow_prompts")
                evaluation = replay["eval_result"] or {}
                row["overflow_replay"] = {"requests": replay["requests"], "error": replay["error"],
                                          "first_request_unchanged": bool(replay["requests"]) and
                                          replay["requests"][0]["broker_sha256"] == first["broker_sha256"],
                                          "evidence_reduced": evaluation.get("evidence_reduced"),
                                          "evaluation_state": evaluation.get("evaluation_state"),
                                          "errors": evaluation.get("errors"),
                                          "prompt_reduction": evaluation.get("prompt_reduction")}
            rows.append(row)
    judged = [r for r in rows if r["requests"]]
    summary = {
        "task": args.task, "run_eval_sha256": sha256_file(new_run_eval), "cases": len(rows),
        "cases_with_judge_request": len(judged), "replay_errors": sum(1 for r in rows if r["error"]),
        "harness_equal": sum(1 for r in rows if r.get("harness_equal") is True),
        "harness_different": [r["case"] for r in rows if r.get("harness_equal") is False]}
    failures = [r["case"] for r in rows if r["error"]] + summary["harness_different"]
    if args.ledger:
        summary["ledger_events"] = len(ledger)
        summary["ledger_matches"] = sum(1 for r in judged if r.get("ledger_match"))
        summary["ledger_unmatched_cases"] = [r["case"] for r in judged if not r.get("ledger_match")]
        summary["ledger_events_not_rebuilt"] = len(set(ledger) - {r["broker_sha256"] for r in judged})
        failures += summary["ledger_unmatched_cases"]
    if args.reference:
        summary["reference_run_eval_sha256"] = reference_hashes
        summary["compare"] = args.compare
        summary["reference_body_matches"] = sum(1 for r in rows if r.get("reference_body_match"))
        summary["reference_prompt_matches"] = sum(1 for r in rows if r.get("reference_prompt_match"))
        summary["reference_field_differences"] = sorted({k for r in rows for k in r.get("reference_field_differences", [])})
        summary["reference_matches"] = sum(1 for r in rows if r.get("reference_match"))
        summary["reference_mismatches"] = [r["case"] for r in rows if r.get("reference_match") is False]
        summary["reference_errors"] = sum(1 for r in rows if r.get("reference_error"))
        failures += summary["reference_mismatches"]
    summary["ok"] = not failures
    if args.report:
        args.report.write_text(json.dumps({"summary": summary, "cases": rows}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    for row in rows:
        if row.get("overflow_replay"):
            print(json.dumps({"case": row["case"], "overflow_replay": row["overflow_replay"]}, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
