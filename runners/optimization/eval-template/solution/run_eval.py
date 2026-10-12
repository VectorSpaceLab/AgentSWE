#!/usr/bin/env python3
"""Evaluator-owned BrowseComp judge and protocol fallback for optimization cases."""

from __future__ import annotations

import argparse
import http.client
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


GRADER_TEMPLATE = """
Judge whether the following [response] to [question] is correct or not based on the precise and unambiguous [correct_answer] below.

[question]: {question}

[response]: {response}

Your judgement must be in the format and criteria specified below:

extracted_final_answer: The final exact answer extracted from the [response]. Put the extracted answer as 'None' if there is no exact, final answer to extract from the response.

[correct_answer]: {correct_answer}

reasoning: Explain why the extracted_final_answer is correct or incorrect based on [correct_answer], focusing only on if there are meaningful differences between [correct_answer] and the extracted_final_answer. Do not comment on any background to the problem, do not attempt to solve the problem, do not argue for any answer different than [correct_answer], focus only on whether the answers match.

correct: Answer 'yes' if extracted_final_answer matches the [correct_answer] given above, or is within a small margin of error for numerical problems. Answer 'no' otherwise, i.e. if there if there is any inconsistency, ambiguity, non-equivalency, or if the extracted answer is incorrect.

confidence: The extracted confidence score between 0|%| and 100|%| from [response]. Put 100 if there is no confidence score available.
""".strip()

GATEWAY_RESPONSES_URL = ""  # Lite: not used by tau3/pinchbench
GATEWAY_DEFAULT_MODEL = "deepseek-flash"
MAX_TRANSPORT_ATTEMPTS = 5
JUDGE_PROTOCOL_VERSION = "browsecomp-simple-evals-v1"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_dotenv(path: Path | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if path is None or not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def first_env(values: dict[str, str], *names: str) -> str:
    for name in names:
        value = os.environ.get(name) or values.get(name)
        if value:
            return value.strip()
    return ""


def read_question(case_id: str, explicit: Path | None) -> str:
    path = explicit or Path(f"/active-case/{case_id}/input.md")
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    return text


def extract_answer(response: str) -> str:
    match = re.search(r"exact answer\s*:\s*(.+?)(?:\n|$)", response, re.I)
    return (match.group(1) if match else response).strip()


def exact_match(response: str, expected: str) -> bool:
    return extract_answer(response).casefold() == expected.strip().casefold()


def cache_key(
    question: str,
    response: str,
    expected: str,
    model: str,
    endpoint: str,
    transport: str,
    prompt_digest: str,
) -> str:
    value = json.dumps(
        {
            "question": question,
            "response": response,
            "answer": expected,
            "model": model,
            "endpoint": endpoint,
            "transport": transport,
            "protocol": JUDGE_PROTOCOL_VERSION,
            "prompt": prompt_digest,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return sha256_text(value)


def load_cache(path: Path | None) -> dict[str, object]:
    if path and path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_cache(path: Path | None, cache: dict[str, object]) -> None:
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def judge_openai(*, prompt: str, model: str, base_url: str, api_key: str, timeout: float) -> dict[str, object]:
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    request = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
        body = json.loads(raw)
        choices = body.get("choices") or []
        text = ""
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            text = str(message.get("content") or choices[0].get("text") or "")
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return {"ok": True, "response": text, "usage": usage, "runtime_seconds": round(time.monotonic() - started, 3), "http_status": 200}
    except HTTPError as exc:
        return {"ok": False, "infrastructure_failure": True, "error": f"judge_http_{exc.code}", "runtime_seconds": round(time.monotonic() - started, 3), "http_status": exc.code}
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "infrastructure_failure": True, "error": f"judge_transport_{type(exc).__name__}", "runtime_seconds": round(time.monotonic() - started, 3)}


def responses_text(body: object) -> str:
    """Extract text from a Responses API result without persisting its raw body."""
    if not isinstance(body, dict):
        raise ValueError("judge response is not an object")
    direct = body.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    chunks: list[str] = []
    output = body.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict) or not isinstance(item.get("content"), list):
                continue
            # Only the answer: providers that return their reasoning as text (a "reasoning" item with
            # reasoning_text parts) would otherwise put the judge's draft "correct:" lines next to its
            # final one and fail the exactly-one-line check. The paper provider returned no reasoning text.
            if item.get("type") == "reasoning":
                continue
            for part in item["content"]:
                if (isinstance(part, dict) and isinstance(part.get("text"), str)
                        and part.get("type") not in {"reasoning_text", "summary_text"}):
                    chunks.append(part["text"])
    if chunks:
        return "".join(chunks)
    raise ValueError("judge response contains no output text")


def judge_responses(
    *, prompt: str, model: str, endpoint: str, api_key: str, timeout: float, effort: str = "medium"
) -> dict[str, object]:
    """Call the authorized GATEWAY Responses API with bounded transient retries."""
    payload = {
        "model": model,
        "reasoning": {"effort": effort},
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    started = time.monotonic()
    last_error = "unknown"
    for attempt in range(1, MAX_TRANSPORT_ATTEMPTS + 1):
        request = Request(
            endpoint,
            data=encoded,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "AgentSWE-Harbor-BrowseComp-Judge/1.0",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            usage = body.get("usage") if isinstance(body, dict) and isinstance(body.get("usage"), dict) else {}
            return {
                "ok": True,
                "response": responses_text(body),
                "usage": usage,
                "runtime_seconds": round(time.monotonic() - started, 3),
                "http_status": 200,
                "transport_attempts": attempt,
            }
        except HTTPError as exc:
            last_error = f"judge_http_{exc.code}"
            if exc.code not in {429, 500, 502, 503, 504}:
                break
        except (
            http.client.IncompleteRead,
            URLError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            last_error = f"judge_transport_{type(exc).__name__}"
        if attempt < MAX_TRANSPORT_ATTEMPTS:
            time.sleep(min(8.0, float(2 ** (attempt - 1))))
    return {
        "ok": False,
        "infrastructure_failure": True,
        "error": last_error,
        "runtime_seconds": round(time.monotonic() - started, 3),
        "transport_attempts": attempt,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-id", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--gold", type=Path)
    parser.add_argument("--question", type=Path)
    parser.add_argument("--credential-file", type=Path, default=Path(os.environ.get("HARNESS_CREDENTIAL_FILE", "")) if os.environ.get("HARNESS_CREDENTIAL_FILE") else None)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    native_result_path = Path("/evaluator/native-result.json")
    if args.benchmark_id in {
        "terminalbench-code-agent-optimization-v1",
        "tau3-tool-agent-optimization-v1",
        "pinchbench-openclaw-agent-optimization-v1",
        "osworld-desktop-agent-optimization-v1",
    }:
        if not native_result_path.is_file():
            result = {
                "schema_version": "2.0", "benchmark_id": args.benchmark_id,
                "case_id": args.case_id, "validity_gate": False, "score": 0,
                "reward": 0, "errors": ["evaluator-owned native result missing"],
                "official_evaluation": False, "infrastructure_failure": True,
                "native_task_available": native_result_path.is_file(),
            }
        else:
            native = json.loads(native_result_path.read_text(encoding="utf-8"))
            official = native.get("official_evaluation") is True
            infrastructure_failure = native.get("infrastructure_failure") is True
            valid = native.get("validity_gate") is True and official and not infrastructure_failure
            score = int(native.get("score", 0)) if valid else 0
            result = {
                "schema_version": "2.0", "benchmark_id": args.benchmark_id,
                "case_id": args.case_id, "validity_gate": valid, "score": score,
                "reward": score / 100, "errors": native.get("errors", []),
                "official_evaluation": official, "infrastructure_failure": infrastructure_failure,
                "deterministic_infrastructure_failure": native.get(
                    "deterministic_infrastructure_failure"
                ) is True,
                "ordinary_agent_failure": native.get("ordinary_agent_failure") is True,
                "native_task_available": native_result_path.is_file(),
                "native_metrics": {
                    "native_task_id": native.get("native_task_id"),
                    "native_task_digest": native.get("native_task_digest"),
                    "prediction_digest": native.get("prediction_digest"),
                    "harbor_job": native.get("harbor_job"),
                    "official_reward": native.get("reward"),
                    "reward_info": native.get("reward_info"),
                    "termination_reason": native.get("termination_reason"),
                    "trajectory_digest": native.get("trajectory_digest"),
                    "message_count": native.get("message_count"),
                    "credential_brokered": native.get("credential_brokered"),
                    "runtime_network": native.get("runtime_network"),
                    "tau_source_commit": native.get("tau_source_commit"),
                    "tau_lock_digest": native.get("tau_lock_digest"),
                    "grading_type": native.get("grading_type"),
                    "grade_breakdown": native.get("grade_breakdown"),
                    "execution_status": native.get("execution_status"),
                    "usage": native.get("usage"),
                    "broker_stats": native.get("broker_stats"),
                    "pinchbench_source_commit": native.get("pinchbench_source_commit"),
                    "openclaw_source_commit": native.get("openclaw_source_commit"),
                    "openclaw_version": native.get("openclaw_version"),
                    "grader_network": native.get("grader_network"),
                    "osworld_source_commit": native.get("source_commit"),
                    "validator": native.get("validator"),
                    "action_count": native.get("action_count"),
                    "model_calls": native.get("model_calls"),
                    "model_tokens": native.get("model_tokens"),
                    "wall_seconds": native.get("wall_seconds"),
                    "vm_image_digest": native.get("vm_image_digest"),
                    "provider": native.get("provider"),
                    "cleanup_verified": native.get("cleanup_verified"),
                },
            }
        (args.output / "eval_result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return 0

    errors: list[str] = []
    resource_budget_exceeded = False
    execution_path = args.predictions.parent / "candidate_execution_evidence.json"
    execution_evidence: dict[str, object] = {}
    if execution_path.is_file():
        try:
            value = json.loads(execution_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                execution_evidence = value
        except (OSError, json.JSONDecodeError):
            pass
    resource_path = args.predictions.parent / "resource_evidence.json"
    resource_evidence: dict[str, object] = {}
    if resource_path.is_file():
        try:
            value = json.loads(resource_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                resource_evidence = value
        except (OSError, json.JSONDecodeError):
            pass
    if resource_evidence.get("resource_mode") == "brokered-v1":
        if execution_evidence.get("case_id") != args.case_id:
            result = {
                "schema_version": "2.2", "benchmark_id": args.benchmark_id,
                "case_id": args.case_id, "validity_gate": False, "score": 0,
                "reward": 0, "errors": ["trusted candidate execution evidence missing"],
                "official_evaluation": False, "infrastructure_failure": True,
            }
            (args.output / "eval_result.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            return 0
        broker_stats = resource_evidence.get("broker_stats")
        stats_error = resource_evidence.get("broker_stats_error")
        infrastructure_failure = bool(stats_error)
        resource_errors: list[str] = []
        if not isinstance(broker_stats, dict):
            infrastructure_failure = True
            resource_errors.append("authenticated candidate resource stats missing")
        else:
            resources = broker_stats.get("resources")
            limits = broker_stats.get("limits")
            if (
                broker_stats.get("credential_brokered") is not True
                or broker_stats.get("candidate_secret_exposed") is not False
                or broker_stats.get("all_requests_settled") is not True
                or broker_stats.get("finalized") is not True
                or limits != {"model": 20, "search": 20, "visit": 20}
                or not isinstance(resources, dict)
            ):
                infrastructure_failure = True
                resource_errors.append("candidate resource broker evidence invalid")
            else:
                for kind in ("model", "search", "visit"):
                    stats = resources.get(kind)
                    if not isinstance(stats, dict):
                        infrastructure_failure = True
                        resource_errors.append(f"{kind} resource stats invalid")
                        continue
                    if int(stats.get("failures", 0)) > 0:
                        infrastructure_failure = True
                        resource_errors.append(f"{kind} provider infrastructure failure")
                    if int(stats.get("calls", 0)) > int(limits[kind]) or stats.get("budget_exceeded") is True:
                        resource_errors.append(f"{kind} resource budget exceeded")
        if infrastructure_failure:
            result = {
                "schema_version": "2.1", "benchmark_id": args.benchmark_id,
                "case_id": args.case_id, "validity_gate": False, "score": 0,
                "reward": 0, "errors": resource_errors or [str(stats_error)],
                "official_evaluation": False, "infrastructure_failure": True,
                "native_metrics": {"candidate_resources": broker_stats},
            }
            (args.output / "eval_result.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            return 0
        if resource_errors:
            # Budget exhaustion is an ordinary official agent outcome. The
            # judge is still run below, but the final score is forced to zero.
            resource_budget_exceeded = True
    candidate_agent_errors: list[str] = []
    if resource_evidence.get("resource_mode") == "brokered-v1":
        if execution_evidence.get("exit_code") != 0:
            candidate_agent_errors.append("candidate harness exited nonzero")
        if execution_evidence.get("timed_out") is True:
            candidate_agent_errors.append("candidate timed out")
        if execution_evidence.get("credential_leak_detected") is True:
            candidate_agent_errors.append("candidate leaked a resource credential")
    rows: list[dict[str, object]] = []
    if args.predictions.is_file():
        for line in args.predictions.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        rows.append(value)
                except json.JSONDecodeError:
                    errors.append("prediction line is not JSON")
    if resource_evidence.get("resource_mode") == "brokered-v1" and (
        len(rows) != 1 or rows[0].get("id") != args.case_id
    ):
        candidate_agent_errors.append("prediction row missing or id mismatch")

    if candidate_agent_errors:
        result = {
            "schema_version": "2.2", "benchmark_id": args.benchmark_id,
            "case_id": args.case_id, "validity_gate": True, "score": 0,
            "reward": 0, "errors": candidate_agent_errors,
            "official_evaluation": True, "infrastructure_failure": False,
            "ordinary_agent_failure": True,
            "native_metrics": {
                "resource_budget_exceeded": resource_budget_exceeded,
                "candidate_resources": resource_evidence.get("broker_stats"),
            },
        }
        (args.output / "eval_result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return 0

    gold: dict[str, object] = {}
    if args.gold and args.gold.is_file():
        try:
            gold = json.loads(args.gold.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            errors.append("gold file is not JSON")
    expected = str(gold.get(args.case_id, {}).get("answer", "")) if isinstance(gold.get(args.case_id), dict) else ""
    response = str(rows[0].get("response", "")) if rows else ""
    question = read_question(args.case_id, args.question)
    exact = exact_match(response, expected) if expected else False
    merged = {**load_dotenv(args.credential_file), **os.environ}
    chat_api_key = first_env(merged, "HARNESS_JUDGE_API_KEY", "BROWSECOMP_JUDGE_API_KEY", "OPENAI_API_KEY", "HARNESS_API_KEY")
    # The runner's evaluator credential file carries the RUNTIME provider (key, Responses URL, model)
    # and the JUDGE effort; the fixed grader uses that provider, as the paper grader used the runtime model.
    gateway_api_key = first_env(merged, "GATEWAY_API_KEY", "AGENTSWE_RUNTIME_API_KEY")
    if chat_api_key:
        transport = "chat_completions"
        api_key = chat_api_key
        model = first_env(merged, "HARNESS_JUDGE_MODEL", "BROWSECOMP_JUDGE_MODEL", "OPENAI_MODEL", "HARNESS_MODEL")
        endpoint = first_env(merged, "HARNESS_JUDGE_BASE_URL", "BROWSECOMP_JUDGE_BASE_URL", "OPENAI_BASE_URL", "HARNESS_BASE_URL") or "https://api.openai.com/v1"
    else:
        transport = "responses"
        api_key = gateway_api_key
        model = first_env(merged, "BROWSECOMP_JUDGE_MODEL", "GATEWAY_MODEL", "AGENTSWE_RUNTIME_MODEL") or GATEWAY_DEFAULT_MODEL
        endpoint = (first_env(merged, "BROWSECOMP_JUDGE_RESPONSES_URL", "GATEWAY_RESPONSES_URL", "AGENTSWE_RUNTIME_RESPONSES_URL")
                    or GATEWAY_RESPONSES_URL)
    judge_effort = first_env(merged, "BROWSECOMP_JUDGE_EFFORT", "AGENTSWE_JUDGE_EFFORT") or "medium"
    prompt = GRADER_TEMPLATE.format(question=question, response=response, correct_answer=expected)
    prompt_digest = sha256_text(prompt)
    key = cache_key(question, response, expected, model, endpoint, transport, prompt_digest)
    cache = load_cache(args.cache)
    judged: dict[str, object] | None = cache.get(key) if isinstance(cache.get(key), dict) else None
    if judged is None:
        if not model or not api_key:
            judged = {"ok": False, "infrastructure_failure": True, "error": "judge_credentials_or_model_missing"}
        elif transport == "responses":
            judged = judge_responses(prompt=prompt, model=model, endpoint=endpoint, api_key=api_key, timeout=args.timeout,
                                     effort=judge_effort)
        else:
            judged = judge_openai(prompt=prompt, model=model, base_url=endpoint, api_key=api_key, timeout=args.timeout)
        # Never poison a deterministic judge cache with a transient provider failure.
        if judged.get("ok") is True:
            cache[key] = judged
            save_cache(args.cache, cache)
    judge_text = str(judged.get("response", "")) if isinstance(judged, dict) else ""
    correct_matches = re.findall(r"^correct\s*:\s*(yes|no)\s*$", judge_text, re.I | re.M)
    judge_ok = bool(isinstance(judged, dict) and judged.get("ok") and len(correct_matches) == 1)
    infrastructure_failure = bool(isinstance(judged, dict) and judged.get("infrastructure_failure"))
    if infrastructure_failure:
        errors.append(str(judged.get("error", "judge infrastructure failure")))
    elif not judge_ok:
        errors.append("judge response missing canonical correct field")
    score = 100 if not resource_budget_exceeded and judge_ok and correct_matches[0].casefold() == "yes" else 0
    result = {
        "schema_version": "2.0",
        "benchmark_id": args.benchmark_id,
        "case_id": args.case_id,
        "validity_gate": not errors,
        "score": score,
        "reward": score / 100,
        "errors": errors,
        "official_evaluation": judge_ok and not infrastructure_failure,
        "infrastructure_failure": infrastructure_failure,
        "native_metrics": {"exact_match_diagnostic": exact, "judge_correct": score == 100, "resource_budget_exceeded": resource_budget_exceeded, "candidate_resources": resource_evidence.get("broker_stats")},
        "judge": {
            "model": model,
            "endpoint": endpoint,
            "transport": transport,
            "effort": judge_effort if transport == "responses" else None,
            "protocol_version": JUDGE_PROTOCOL_VERSION,
            "prompt_digest": prompt_digest,
            "cache_key": key,
            "response_digest": sha256_text(judge_text) if judge_text else None,
            "usage": judged.get("usage", {}) if isinstance(judged, dict) else {},
            "runtime_seconds": judged.get("runtime_seconds") if isinstance(judged, dict) else None,
            "transport_attempts": judged.get("transport_attempts") if isinstance(judged, dict) else None,
        },
    }
    (args.output / "eval_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
