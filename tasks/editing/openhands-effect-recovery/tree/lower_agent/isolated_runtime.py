"""Trusted model/world loop with a network-none Candidate product server."""
from __future__ import annotations
import hashlib
import argparse
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time


class CandidateResponseTruncated(RuntimeError):
    """The Candidate's own max_output_tokens cut the model answer short."""


class CaseBudgetReserveReached(RuntimeError):
    """The evaluator refused to dispatch a request the case budget cannot settle.

    Exit code 1, like CandidateResponseTruncated, so 124/125 do not classify it as
    lower-agent infrastructure.  0921: the controller reads the guard records this
    raise leaves behind (budget_reserve_stop.json, dispatch_guard.json,
    artifact_reask.json) and re-attributes it to the evaluator
    (classification evaluator_budget_truncation), because a case the evaluator's own
    budget ended was not measured and is not a Candidate zero.
    """
import urllib.error
import urllib.request

sys.path.append("@@AGENTSWE_EDITING_CONTROL@@")
from responses_stream import strict_json
from isolated_product import product_server_source
from owned_resources import run_owned, _ambient_aggregate, verify_aggregate, MEMORY_BYTES

# The paper pinned the id of its builder image (sha256:3bebcf4e7770...); a release install pins the builder image its setup built.
PINNED_IMAGE = "@@AGENTSWE_BUILDER_CODEX_IMAGE_ID@@"
MODEL_STEP_RESERVE_SECONDS = 150  # budget kept for the final-artifact request (0919)
# Pre-dispatch deadline guard for that final-artifact request itself (2026-09-20).
# The action loop above reserves MODEL_STEP_RESERVE_SECONDS for it, but the loop can
# also end on a terminal model decision or on the step limit at any remaining budget,
# and the product actions after the last check spend more.  A request started with
# too little left is killed in flight by the case deadline and booked usage_unknown,
# which the shared readiness normalizers refuse (a bare TimeoutError names no
# provider side, so the D13 recovered-transport carve-out does not cover it either).
# Same number as the loop reserve on purpose: that constant is this tree's own stated
# worst case for this one request, and openhands' ledger records no durations.
# Measured 2026-09-20 from response-file mtimes across 0919-fw-001, 0919-ds-006
# and the twelve hidden cases of the two 0919 formal openhands runs.  Product
# actions cost 9-36 ms, so a turn is its model request and the mtime deltas
# measure the requests directly.  Single artifact request, n=13: median 43.4,
# p90 90.4, max 105.7.  (A fourteenth observation of 218.9 s is two requests --
# that case needed a format repair -- and the repair has its own budget gate.)
MODEL_ARTIFACT_RESERVE_SECONDS = 100  # what the loop keeps back for the final-artifact request
# The final-artifact request measured end to end over the six hidden cases of
# 0920-fh-003 (response-file mtime minus the previous model response): 13.5, 49.6,
# 56.6, 60.5, 66.5 and 121.1 s, median 58.6; the 0919/0920 sample (n=13) was median
# 43.4, p90 90.4, max 105.7.  100 s covers the p90 of both samples.
MODEL_ARTIFACT_MINIMUM_SECONDS = 60  # below this the request is refused before dispatch
# 0921.  The loop used to hold back a FIXED 135 + 75 = 210 s, i.e. 37% of the 570 s
# work budget, and 4 of 6 hidden cases and 9 of 10 dev runs of 0920-fh-003 ended on
# that guard with a plan half finished.  The 75 s turn allowance existed because a
# turn admitted just above the line can spend the artifact reserve it was checked
# against (readiness 0920-hd-001).  That allowance is now measured from THIS case's
# own completed turns instead of assumed: fh-003 turns were median 23.3, p90 73.9,
# max 107.7 s (n=57), so a fixed 75 taxed every fast case for the worst slow one.
# Fixed floor: 100 + 20 = 120 s (570 - 120 = 450 s of action turns, was 360).
MODEL_TURN_PAD_FLOOR_SECONDS = 20
MODEL_TURN_PAD_CEILING_SECONDS = 90
MODEL_ACTION_LOOP_RESERVE_SECONDS = MODEL_ARTIFACT_RESERVE_SECONDS + MODEL_TURN_PAD_FLOOR_SECONDS
ARTIFACT_REASK_TURNS = 1  # one NEW logical request when the artifact reply is an action, never a resample
# The re-ask floor is lower than the first dispatch floor on purpose: by the time
# it is reached the case is already lost -- the answer in hand is not an artifact --
# and a re-ask the case deadline cuts costs one tolerated deadline row and leaves the
# case exactly where it already was, attributed to the evaluator.  Trying is nearly
# free; not trying forfeits the case.  0920-fh-003 round 2 dev_002 had 58.7 s left.
ARTIFACT_REASK_MINIMUM_SECONDS = 45
ARTIFACT_REASK_SCHEMA = "agentswe-openhands-artifact-reask/v1"


def _broker_error_type(exception: BaseException) -> str | None:
    """The `type` the candidate broker put in an HTTP error body, if any (package 120)."""
    if not isinstance(exception, urllib.error.HTTPError):
        return None
    try:
        body = exception.read()
        value = json.loads(body.decode("utf-8", "replace")) if body else {}
    except Exception:
        return None
    return value.get("type") if isinstance(value, dict) else None


def action_turn_pad(turn_seconds):
    """Wall clock to keep back for the turn the loop is about to admit.

    Measured from this case's own completed turns (model request + product action +
    world calls), as a conservative p75, clamped into [floor, ceiling].  With no
    observation yet the budget is not the binding constraint, so the ceiling is used.
    """
    if not turn_seconds:
        return MODEL_TURN_PAD_CEILING_SECONDS
    ordered = sorted(turn_seconds)
    p75 = ordered[min(len(ordered) - 1, (len(ordered) * 3) // 4)]
    return max(MODEL_TURN_PAD_FLOOR_SECONDS, min(MODEL_TURN_PAD_CEILING_SECONDS, p75))


def artifact_mode_defect(artifact):
    """Why a parsed final-artifact reply is not the artifact that request asked for.

    A reply that is a well-formed action object never reached the JSON format-repair
    path (it parses), was written to agent_result.json, failed binding validation and
    was booked against the Candidate at 0/100 -- 1 of 6 hidden and 6 of 10 dev runs of
    0920-fh-003.  Naming the defect is what makes one explicit re-ask possible.
    """
    if not isinstance(artifact, dict):
        return "the final-artifact reply is not a JSON object"
    kind = artifact.get("kind")
    if artifact.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        if isinstance(kind, str):
            return ("the final-artifact request was answered with an action-loop choice (kind=%r), "
                    "not with the agent artifact" % kind)
        return "the final-artifact reply carries no %s schema_version" % ARTIFACT_SCHEMA_VERSION
    return None


def artifact_mode_prompt(prompt, defect, previous_text):
    """The same artifact request, told explicitly that the action loop is over.

    The instruction is inserted before the prompt's final line: model_origin.
    completed_origin parses exactly that last line as the trajectory context, so it
    has to stay last.  The rejected reply travels as a JSON string, byte for byte.
    """
    instruction = ("The action loop for this case is finished and this request is the FINAL ARTIFACT"
        " request. The previous reply could not be accepted: " + defect + "."
        "\nDo not answer with an action or a terminal choice: no product action will be dispatched"
        " from it and no further observation will be returned. Author the artifact now, from the"
        " actual observed product behavior above, reporting failures honestly."
        "\nReturn exactly one strict JSON object and nothing else: no prose, no markdown fence, with"
        " the schema_version, case_id, model, reasoning_effort, product, product_entry, actions,"
        " observations, decision, rationale, nonce_digest and trajectory_digest fields this request"
        " already specified."
        "\nPrevious reply, verbatim, as a JSON string:\n" + json.dumps(previous_text))
    head, separator, tail = prompt.rpartition("\n")
    if not separator:
        return prompt + "\n" + instruction
    return head + "\n" + instruction + "\n" + tail


def stash_artifact_capture(capture, label):
    """Move a rejected final-artifact capture aside with every sidecar it owns."""
    moved = []
    original = capture.with_name(capture.stem + ".original" + capture.suffix)
    pairs = ((capture, capture.with_name(capture.stem + "." + label + capture.suffix)),
             (original, capture.with_name(capture.stem + "." + label + ".original" + capture.suffix)))
    for source_base, target_base in pairs:
        for suffix in ("", ".request.json", ".payload.json", ".trailing_json.json", ".format_repair.json"):
            source = Path(str(source_base) + suffix)
            if source.exists():
                source.rename(Path(str(target_base) + suffix))
                moved.append(source.name)
    return moved
DISPATCH_GUARD_SCHEMA = "agentswe-openhands-dispatch-guard/v1"
WORKER_EXIT_MARGIN_SECONDS = 40  # graceful failure write + container/world teardown before the supervisor hard kill (0919)
FORMAT_REPAIR_TURNS = 1  # at most one NEW logical request per unparsable reply; never a resample of the same request (0919)


def cleanup_container(name):
    query = ["docker", "ps", "-aq", "--filter", "name=^" + name + "$", "--filter", "label=agentswe.owner=0909-owner-b", "--filter", "label=agentswe.task=openhands"]
    before = subprocess.run(query, capture_output=True, text=True, timeout=30, check=False)
    report = {"container_name": name, "query_exit_code": before.returncode, "container_absent": False}
    if before.returncode != 0: return report
    if before.stdout.strip():
        inspect = subprocess.run(["docker", "inspect", name], capture_output=True, text=True, timeout=30, check=False)
        if inspect.returncode != 0: return report
        actual = json.loads(inspect.stdout)[0]
        labels = actual.get("Config", {}).get("Labels", {})
        if actual.get("Name") != "/" + name or labels.get("agentswe.owner") != "0909-owner-b" or labels.get("agentswe.task") != "openhands": return report
        removed = subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=30, check=False)
        report["remove_exit_code"] = removed.returncode
        # A failed `rm` is the reason to check whether the container is gone,
        # not a reason to stop checking. The case container runs with --rm, so
        # the daemon removes it on exit and `rm -f` can lose that race while the
        # container is already absent -- the outcome cleanup wanted. Returning
        # here left container_absent False either way, which turned a completed,
        # scoreable round into an evaluator infrastructure failure and burned
        # the candidate tree permanently. Record what docker said, then verify.
        if removed.returncode != 0:
            report["remove_stdout"] = removed.stdout[-500:]
            report["remove_stderr"] = removed.stderr[-500:]
    # 116h: on docker 29 `rm -f` can answer "removal ... is already in progress" and the
    # very next inspect still lists the container for a few seconds.  Poll (bounded) until
    # the daemon has actually dropped it instead of judging from one immediate look.
    poll_started = time.monotonic()
    while True:
        after = subprocess.run(["docker", "inspect", name], capture_output=True, text=True, timeout=30, check=False)
        final = subprocess.run(query, capture_output=True, text=True, timeout=30, check=False)
        absent = after.returncode != 0 and ("no such object" in after.stderr.lower() or "no such container" in after.stderr.lower()) and final.returncode == 0 and not final.stdout.strip()
        if absent or time.monotonic() - poll_started > 60: break
        time.sleep(2)
    report.update(inspect_after_exit_code=after.returncode, final_query_exit_code=final.returncode,
        container_absent=absent, absence_poll_seconds=round(time.monotonic() - poll_started, 1))
    return report


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def request(path: Path, route: str, value: dict, timeout: float) -> dict:
    connection = UnixHTTPConnection(path, timeout)
    try:
        connection.request("POST", route, json.dumps(value).encode(), {"content-type": "application/json"})
        response = connection.getresponse()
        data = response.read(2_000_001)
        if len(data) > 2_000_000:
            raise ValueError("oversized product response")
        result = json.loads(data)
        if not isinstance(result, dict): raise ValueError("invalid product response")
        return result
    finally:
        connection.close()


def safe(value):
    if isinstance(value, list): return [safe(item) for item in value]
    if isinstance(value, dict):
        import re
        return {key: "<redacted>" if re.search("token|secret|credential|authorization|password|bytes", key, re.I) else safe(item) for key, item in value.items()}
    return value


def write(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


from model_origin import decision as model_decision, completed_origin, artifact_from_concatenated, ARTIFACT_SCHEMA_VERSION


def format_repair_record(capture):
    """The compact format-repair claim for one capture, or None when there was none."""
    if capture is None: return None
    path = Path(str(capture) + ".format_repair.json")
    if not path.is_file(): return None
    record = json.loads(path.read_text())
    return {key: record.get(key) for key in ("attempted", "original_sha256", "repaired_sha256", "parser_error")}


def artifact_trailing_record(capture):
    """The compact not-parsed-remainder claim for one capture, or None when there was none."""
    if capture is None: return None
    path = Path(str(capture) + ".trailing_json.json")
    if not path.is_file(): return None
    record = json.loads(path.read_text())
    return {key: record.get(key) for key in
            ("trailing_bytes", "trailing_sha256", "parsed_sha256", "top_level_values", "parser_error")}


def repair_prompt(prompt, text, parser_error):
    """One new logical request: the same context, plus the unparsed reply verbatim.

    The repair instruction is inserted before the prompt's final line, never
    after it.  model_origin.completed_origin binds the final artifact by parsing
    exactly the last line of the captured payload as the evaluator's trajectory
    context, so the request that produced the canonical capture has to keep that
    line last.  The unparsed reply travels as a JSON string: that preserves it
    byte for byte and cannot introduce a new final line.
    """
    instruction = ("The previous reply to this request could not be parsed by the evaluator and was discarded."
        "\nParser error: " + parser_error +
        "\nPrevious reply, verbatim, as a JSON string:\n" + json.dumps(text) +
        "\nReturn that same content once more as a single strict JSON object and nothing else:"
        " no prose, no explanation, no markdown fence, no text before or after the object."
        " Do not change any observed value, action or wording; repair only the JSON syntax.")
    head, separator, tail = prompt.rpartition("\n")
    if not separator: return prompt + "\n" + instruction
    return head + "\n" + instruction + "\n" + tail


def model_json(lower, endpoint, prompt, case_id, phase_name, remaining, capture=None, *, evaluation_scope=None, repair_of=None):
    # metadata stays a local label: the broker pops it before it hashes the logical
    # request and before it forwards anything upstream, so naming the repair here
    # cannot change request identity, accounting or what the provider is asked.
    payload = {"model": lower.MODEL, "reasoning": {"effort": lower.EFFORT}, "input": prompt,
               "metadata": {"case_id": case_id, "phase": phase_name,
                            **({"format_repair": repair_of["repair_index"]} if repair_of else {})}}
    request_bytes = json.dumps(payload).encode()
    intent = Path(str(capture) + ".request.json") if capture else None
    if intent:
        payload_path = Path(str(capture) + ".payload.json")
        with payload_path.open("x") as handle: json.dump(payload, handle)
        with intent.open("x") as handle:
            json.dump({"request_sha256": hashlib.sha256(request_bytes).hexdigest(), "state": "submitted_or_unknown", "case_id": case_id, "phase": phase_name, "automatic_retry": False, "payload_sha256": hashlib.sha256(payload_path.read_bytes()).hexdigest()}, handle)
    message = urllib.request.Request(endpoint, data=request_bytes, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer broker-only-placeholder",
                 "X-AgentSWE-Case-Deadline": str(time.monotonic()+remaining()),
                 **({"X-AgentSWE-Evaluation": evaluation_scope} if evaluation_scope else {})})
    with urllib.request.urlopen(message, timeout=max(.001, remaining())) as response:
        body = strict_json(response.read())
    if (body.get("status") == "incomplete" and isinstance(body.get("incomplete_details"), dict)
            and body["incomplete_details"].get("reason") == "max_output_tokens" and body.get("error") is None
            and isinstance(body.get("id"), str) and body["id"]):
        # The provider finished inside the Candidate's own output budget and the broker
        # forwarded it as-is (decision 2026-09-19): a truncated answer is the Candidate's
        # failure, not provider or evaluator infrastructure.
        raise CandidateResponseTruncated(f"{phase_name}: model response truncated at the Candidate's max_output_tokens")
    if body.get("status") != "completed" or not isinstance(body.get("id"), str) or not body["id"]:
        raise urllib.error.URLError("broker response lacks typed completed status; no automatic retry")
    # Only message items carry the answer. A reasoning item is the model's
    # thinking; concatenating it in front of the JSON makes json.loads fail at
    # character 0. Kept identical in intent to code_eval.response_text_from_body
    # and to this task's own TypeScript path.
    text = body.get("output_text") or "".join(
        item.get("text", "")
        for block in body.get("output", []) if block.get("type") == "message"
        for item in block.get("content", []) if item.get("type") == "output_text")
    if capture is not None:
        with Path(capture).open("x", encoding="utf-8") as handle:
            handle.write(text)
    if intent:
        recorded = json.loads(intent.read_text()); recorded.update(state="completed", response_sha256=hashlib.sha256(text.encode()).hexdigest())
        write(intent, recorded)
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict): raise ValueError("model JSON must be an object")
    except ValueError as parser_error:
        # First: a final-artifact reply that carries the authored artifact PLUS a
        # second complete top-level object (a terminal `finish` choice in front of
        # it) or trailing prose. Both halves already parse, so there is nothing for
        # a repair turn to repair and no provider call to spend -- and the repair
        # turn cannot run this late in the case budget anyway. Bind the one object
        # that carries the schema_version the artifact prompt asked for; if none or
        # several do, choose nothing and fall through to the paths below.
        # The canonical capture keeps every original byte: no new request produced
        # these bytes, and the local intent's response_sha256, completed_origin's
        # capture digest and its historical-proof comparison all read them.
        # model_origin.value applies the same rule when it re-reads the capture.
        selected = artifact_from_concatenated(cleaned) if phase_name == "artifact" else None
        if selected is not None:
            parsed, parsed_start, parsed_end, values = selected
            chunks = [(0, parsed_start, cleaned[:parsed_start]), (parsed_end, len(cleaned), cleaned[parsed_end:])]
            regions = [{"span": [start, end], "bytes": len(chunk.encode()),
                        "sha256": hashlib.sha256(chunk.encode()).hexdigest()}
                       for start, end, chunk in chunks if chunk.strip()]
            joined = "".join(chunk for _, _, chunk in chunks if chunk.strip())
            record = {"schema_version": "agentswe-openhands-artifact-trailing/v1",
                      "case_id": case_id, "phase": phase_name,
                      "parser_error": f"{type(parser_error).__name__}: {parser_error}",
                      "top_level_values": len(values), "parsed_span": [parsed_start, parsed_end],
                      "parsed_sha256": hashlib.sha256(cleaned[parsed_start:parsed_end].encode()).hexdigest(),
                      "capture_sha256": hashlib.sha256(text.encode()).hexdigest(),
                      "capture_rewritten": False,
                      "trailing_bytes": len(joined.encode()),
                      "trailing_sha256": hashlib.sha256(joined.encode()).hexdigest(),
                      "trailing_regions": regions}
            if capture is not None: write(Path(str(capture) + ".trailing_json.json"), record)
            return parsed, hashlib.sha256(text.encode()).hexdigest()
        # A completed but unparsable reply is one formatting failure of the lower
        # model, not a transport fault, and today it kills the whole run. Resampling
        # this same request stays forbidden (broker protocol: inner_retries 0,
        # max_upstream_attempts_per_transport 1), so send exactly one NEW logical
        # request that hands the unparsed bytes and the parser error back to the
        # model. The repaired reply becomes the canonical capture and the original
        # is kept beside it, so the bytes every evidence check reads stay the bytes
        # that were actually parsed. If the repair also fails, exit as before.
        if capture is None or repair_of is not None or FORMAT_REPAIR_TURNS < 1: raise
        try: budget = remaining()
        except TimeoutError: raise parser_error from None
        if budget < MODEL_STEP_RESERVE_SECONDS: raise parser_error from None
        described = f"{type(parser_error).__name__}: {parser_error}"
        capture = Path(capture)
        original = capture.with_name(capture.stem + ".original" + capture.suffix)
        record = {"schema_version": "agentswe-openhands-format-repair/v1", "attempted": True, "repaired": False,
                  "case_id": case_id, "phase": phase_name, "repair_index": FORMAT_REPAIR_TURNS,
                  "parser_error": described, "original_capture": original.name,
                  "original_sha256": hashlib.sha256(text.encode()).hexdigest(), "repaired_sha256": None}
        for suffix in ("", ".request.json", ".payload.json"):
            source = Path(str(capture) + suffix)
            if source.exists(): source.rename(Path(str(original) + suffix))
        write(Path(str(capture) + ".format_repair.json"), record)
        try:
            parsed, repaired_digest = model_json(lower, endpoint, repair_prompt(prompt, text, described), case_id,
                phase_name, remaining, capture, evaluation_scope=evaluation_scope,
                repair_of={"repair_index": FORMAT_REPAIR_TURNS, "original_sha256": record["original_sha256"], "parser_error": described})
        except ValueError as repair_error:
            record["repair_parser_error"] = f"{type(repair_error).__name__}: {repair_error}"
            write(Path(str(capture) + ".format_repair.json"), record)
            raise parser_error from None
        record.update(repaired=True, repaired_sha256=repaired_digest)
        write(Path(str(capture) + ".format_repair.json"), record)
        return parsed, repaired_digest
    return parsed, hashlib.sha256(text.encode()).hexdigest()


def run_isolated(lower, *, repo: Path, case_id: str, task: str, nonce: str,
                 fixture: dict, output: Path, endpoint: str, timeout: int,
                 diagnostic_actions: list[dict] | None = None):
    if not 0 < timeout <= 600: raise ValueError("case total budget must be within 600 seconds")
    # One identity scope per evaluation. Two Candidates run the same case from the
    # same pristine repository, so their first request is byte-identical; sharing a
    # logical-request identity across them let one transient upstream failure refuse
    # every later Candidate for the rest of the run.
    evaluation_scope = hashlib.sha256(
        ("agentswe-lower-evaluation/v1\x00" + case_id + "\x00" + str(Path(output).resolve())).encode()).hexdigest()
    aggregate_unit = _ambient_aggregate()
    if aggregate_unit is None: raise RuntimeError("lower worker is outside its owned aggregate resource parent")
    aggregate_parent = {"unit": aggregate_unit, "cgroup": "/" + aggregate_unit, "memory_bytes": MEMORY_BYTES}
    verify_aggregate(aggregate_parent)
    started = time.monotonic()
    def remaining():
        value = timeout - (time.monotonic() - started)
        if value <= 0: raise TimeoutError("case total wall budget exhausted")
        return value
    def phase(name, **details):
        write(output / "execution_phase.json", {"phase": name, "observed_at_ms": round(time.time() * 1000), **details})
    # Unix socket addresses have a short OS length limit. Only this fresh
    # public bridge directory is mounted, never its private sibling or logs.
    ipc_root = Path(tempfile.mkdtemp(prefix="agentswe-oh-0909-"))
    public = ipc_root / "public"; public.mkdir()
    private = ipc_root / "private"; private.mkdir()
    product_socket, world_socket, control_socket = public / "product.sock", public / "world.sock", private / "world.sock"
    config = {"case_id": case_id, "nonce": nonce, "fixture": fixture,
              "public_socket": str(world_socket), "private_socket": str(control_socket), "product_socket": str(product_socket)}
    config_path = output / "private_world_config.json"; write(config_path, config)
    driver = repo / ".agentswe_lower_case.test.ts"
    driver.write_text(product_server_source(lower, case_id, task, nonce))
    shutil.copy2(Path(__file__).with_name("product_observation.ts"), repo / ".agentswe_product_observation.ts")
    for leaf in (".vite", ".vite-temp"):
        (repo / "node_modules" / leaf).mkdir(exist_ok=True)
    name = "agentswe-0909-oh-" + secrets.token_hex(8)
    command = ["docker", "run", "--rm", "--name", name, "--label", "agentswe.owner=0909-owner-b", "--label", "agentswe.task=openhands",
        "--network", "none", "--cgroup-parent", aggregate_unit, "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=256", "--memory=4g", "--cpus=2", "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g", "--tmpfs", "/candidate/node_modules/.vite:rw,nosuid,nodev,size=128m",
        "--tmpfs", "/candidate/node_modules/.vite-temp:rw,nosuid,nodev,size=128m", "-v", f"{repo}:/candidate:ro", "-v", f"{public}:/bridge:rw", "-w", "/candidate",
        "-e", "HOME=/tmp/home", "-e", "CI=1", "-e", "VITEST_POOL_SIZE=1", "-e", "OPENAI_API_KEY=broker-only-placeholder", "-e", "DEEPSEEK_API_KEY=broker-only-placeholder",
        PINNED_IMAGE, "node", "node_modules/vitest/vitest.mjs", "run", ".agentswe_lower_case.test.ts", "--environment", "jsdom", "--reporter", "dot"]
    world_process = product_process = None
    logs = []
    steps, native_steps = [], []
    code, error, stage = 0, "", "preflight"
    cleanup = {}
    try:
        phase("runtime_preflight")
        subprocess.run(["docker", "image", "inspect", PINNED_IMAGE], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=min(30, remaining()))
        world_log = (output / "world_service.log").open("w"); logs.append(world_log)
        world_process = subprocess.Popen(["node", "--experimental-strip-types", str(Path(__file__).with_name("world_service.ts")), str(config_path)],
            stdout=world_log, stderr=world_log, env=lower.sanitized_environment())
        deadline = time.monotonic() + min(15, remaining())
        while not (world_socket.exists() and control_socket.exists()):
            if world_process.poll() is not None or time.monotonic() > deadline: raise RuntimeError("trusted world process startup failed")
            time.sleep(.05)
        # Inspect the actual boundary: private oracle paths are unavailable
        # from the public endpoint before any Candidate or provider execution.
        denied = request(world_socket, "/snapshot", {}, min(5, remaining()))
        if not denied.get("error"): raise RuntimeError("private oracle exposed on product socket")
        product_log = (output / "product_runtime.log").open("w"); logs.append(product_log)
        product_process = subprocess.Popen(command, stdout=product_log, stderr=product_log, env=lower.sanitized_environment())
        deadline = time.monotonic() + min(60, remaining())
        while not product_socket.exists():
            if product_process.poll() is not None or time.monotonic() > deadline: raise RuntimeError("Candidate product server startup failed; inspect captured log")
            time.sleep(.05)
        inspected = subprocess.run(["docker", "inspect", name], capture_output=True, text=True, check=True, timeout=min(5, remaining()))
        container_info = json.loads(inspected.stdout)[0]
        container_pid = int(container_info["State"]["Pid"])
        actual_container_cgroup = next(line.split(":", 2)[2] for line in Path(f"/proc/{container_pid}/cgroup").read_text().splitlines() if line.startswith("0::"))
        if container_info["HostConfig"].get("CgroupParent") != aggregate_unit or not actual_container_cgroup.startswith(aggregate_parent["cgroup"] + "/"):
            raise RuntimeError("actual Candidate Docker cgroup escaped the aggregate budget")
        aggregate_observation = verify_aggregate(aggregate_parent)
        state = request(product_socket, "/state", {}, min(30, remaining())).get("result", {})
        view = request(world_socket, "/init", {}, min(10, remaining()))["result"]
        write(output / "environment_preflight.json", {"valid": True, "validated_by": "evaluator", "network": "none", "public_oracle_request_denied": True,
            "actual_product_server_ready": True, "aggregate_parent": aggregate_parent, "aggregate_observation": aggregate_observation,
            "actual_container_cgroup": actual_container_cgroup, "world_pid": world_process.pid, "worker_pid": os.getpid(),
            "worker_cgroup": Path("/proc/self/cgroup").read_text(), "world_cgroup": Path(f"/proc/{world_process.pid}/cgroup").read_text(), "image_digest": PINNED_IMAGE, "candidate_sees_model_endpoint": False,
            "configuration_delta": "existing fixed Node24 Builder image used only as restricted runtime; no Builder process, privileged mount or provider credential"})
        turn_seconds: list[float] = []
        for index in range(12):
            turn_started = time.monotonic()
            if diagnostic_actions is not None:
                if index == len(diagnostic_actions): break
                choice, response_digest, action_capture = diagnostic_actions[index], None, None
            else:
                turn_pad = action_turn_pad(turn_seconds)
                stop_reserve = MODEL_ARTIFACT_RESERVE_SECONDS + turn_pad
                if remaining() < stop_reserve:
                    # Keep enough budget for the final-artifact request instead of
                    # being killed mid-request by the case deadline -- and enough
                    # for THIS turn on top of it, so admitting a turn can no longer
                    # consume the artifact reserve it was just checked against.  The
                    # turn allowance is this case's own measured p75 turn (0921), not
                    # a fixed 75 s: the loop stop is evaluator-side, so every second
                    # held back that the case did not need is a turn the plan loses.
                    write(output / "budget_reserve_stop.json", {"step": index + 1, "remaining_seconds": round(remaining(), 1),
                          "reserve_seconds": round(stop_reserve, 1),
                          "artifact_reserve_seconds": MODEL_ARTIFACT_RESERVE_SECONDS,
                          "turn_allowance_seconds": round(turn_pad, 1),
                          "fixed_reserve_floor_seconds": MODEL_ACTION_LOOP_RESERVE_SECONDS,
                          "observed_turn_seconds": [round(value, 1) for value in turn_seconds],
                          "reason": "case_budget_reserve_reached"})
                    break
                stage = "model_request"; phase(stage, step=index + 1)
                prompt = lower.model_prompt(case_id, task, nonce) + "\nCurrent user-visible runtime notice:\n" + json.dumps(view["notice"]) + "\nPrevious sanitized product trajectory:\n" + json.dumps(steps)
                action_capture = output / f"model_action_{index + 1:03d}_response.txt"
                choice, response_digest = model_json(lower, endpoint, prompt, case_id, "action", remaining, action_capture, evaluation_scope=evaluation_scope)
            stage = "model_choice_validation"; phase(stage, step=index + 1, response_sha256=response_digest)
            selected = model_decision(choice, lower.ACTION_NAMES)
            if selected["kind"] == "terminal":
                write(output / "model_terminal_decision.json", {**selected, "step": index + 1, "response_sha256": response_digest})
                break
            action, supplied = selected["action"], selected["arguments"]
            stage = "product_action"; phase(stage, action=action, step=index + 1)
            action_started = time.monotonic()
            view = request(control_socket, "/before", {"action": action, "inspection": state.get("inspection")}, min(30, remaining()))["result"]
            attempted = action in lower.ACTION_NAMES
            result = request(product_socket, "/action", {"choice": {"action": action, "arguments": supplied}, "world": view}, min(60, remaining()))
            if result.get("error"):
                details = result["error"]
                observation = {"status": "action_rejected", **(details if isinstance(details, dict) else {"message": str(details)})}
                state = {key: result.get(key) for key in ("inspection", "storage")}
                raw_result, dispatched = {"workspace_projection": result.get("workspace_projection"), "observed_error": result["error"]}, False
            else:
                state = result["result"]; observation = state.get("observation"); raw_result = state.get("result"); dispatched = state.get("dispatched") is True
            view = request(control_socket, "/after", {"action": action, "result": raw_result, "storage": state.get("storage")}, min(30, remaining()))["result"]
            state = request(product_socket, "/state", {}, min(30, remaining()))["result"]
            view = request(control_socket, "/observe-state", {"storage": state.get("storage")}, min(30, remaining()))["result"]
            if isinstance(observation, dict):
                observation = {**observation, "current_product_state": safe({key: value for key, value in state.items() if key not in {"storage", "storage_fault_observations"}})}
            write(output / "final_product_state.json", safe(state))
            action_evidence = result.get("action_evidence") or result.get("result", {}).get("action_evidence")
            native_steps.append({"step": index + 1, "action": action, "actual_product_response": safe(result), "observed_at_ns": time.time_ns()})
            step = {"step": index + 1, "action": action, "arguments": safe(supplied), "product_method": action if dispatched else None,
                "product_entry": lower.PRODUCT_ENTRY, "product_surface": lower.PRODUCTION_SERVICE_ENTRY if "production" in action else lower.PRODUCT_ENTRY,
                "dispatched": dispatched, "attempted": attempted, "action_evidence": safe(action_evidence), "action_duration_ms": round((time.monotonic() - action_started) * 1000), "model_response_sha256": response_digest, "observation": observation}
            # Only present when this decision needed a repair turn, so a run without
            # one keeps a byte-identical trajectory and trajectory_digest.
            action_repair = format_repair_record(action_capture)
            if action_repair: step["format_repair"] = action_repair
            steps.append(step)
            trajectory_digest = hashlib.sha256(lower.canonical_json(steps).encode()).hexdigest()
            write(output / "trajectory.json", {"schema_version": "agentswe-openhands-trajectory/v1", "case_id": case_id, "model": lower.MODEL, "reasoning_effort": lower.EFFORT,
                "product_entry": lower.PRODUCT_ENTRY, "steps": steps, "trajectory_digest": trajectory_digest, "provider_free_diagnostic": diagnostic_actions is not None})
            write(output / "case_world.json", safe(request(control_socket, "/snapshot", {}, min(10, remaining()))["result"]))
            write(output / "native_product_observations.json", native_steps)
            turn_seconds.append(time.monotonic() - turn_started)
        if diagnostic_actions is None:
            # Pre-dispatch deadline guard for the final-artifact request.  Written
            # unconditionally so the judge can see that the guard was evaluated and
            # whether it fired; a refusal ends the case here with no artifact, and
            # the existing artifact-absence path books the Candidate outcome.
            artifact_remaining = remaining()
            # 0921: refuse only below the hard minimum, not below the full reserve.
            # The reserve is what the LOOP keeps back; once the loop has stopped, a
            # request that still fits is worth making -- refusing it forfeits the case
            # for a Candidate that has already done the work.
            artifact_refused = artifact_remaining < MODEL_ARTIFACT_MINIMUM_SECONDS
            stop_path = output / "budget_reserve_stop.json"
            write(output / "dispatch_guard.json", {
                "schema_version": DISPATCH_GUARD_SCHEMA, "request": "artifact",
                "refused": artifact_refused,
                "reason": "case_budget_reserve_reached" if artifact_refused else "within_reserve",
                "remaining_seconds": round(artifact_remaining, 1),
                "reserve_seconds": MODEL_ARTIFACT_RESERVE_SECONDS,
                "minimum_seconds": MODEL_ARTIFACT_MINIMUM_SECONDS,
                "below_reserve": artifact_remaining < MODEL_ARTIFACT_RESERVE_SECONDS,
                "turns_completed": len(steps),
                "action_loop_stop": json.loads(stop_path.read_text()) if stop_path.is_file() else None,
                "stopped_by": "evaluator_budget_guard" if artifact_refused else None})
            if artifact_refused:
                raise CaseBudgetReserveReached(
                    "final-artifact request not dispatched: "
                    f"{round(artifact_remaining, 1)}s remaining is below the "
                    f"{MODEL_ARTIFACT_MINIMUM_SECONDS}s minimum")
            stage = "artifact_model_request"; phase(stage, action_count=len(steps))
            trajectory_digest = hashlib.sha256(lower.canonical_json(steps).encode()).hexdigest()
            nonce_digest = hashlib.sha256(lower.canonical_json(nonce).encode()).hexdigest()
            prompt = lower.model_prompt(case_id, task, nonce) + "\nAuthor the final artifact from actual observed product behavior, including failures honestly. Return JSON with schema_version='agentswe-openhands-agent-result/v1', case_id='" + case_id + "', model='" + lower.MODEL + "', reasoning_effort='" + lower.EFFORT + "', product='@openhands/agent-canvas', product_entry='" + lower.PRODUCT_ENTRY + "', actions (exact list), observations (objects with action and result copied exactly), decision, rationale, nonce_digest, trajectory_digest.\n" + json.dumps({"trajectory_digest": trajectory_digest, "nonce_digest": nonce_digest, "actual_trajectory": steps})
            capture = output / "model_final_response.txt"

            def ask_artifact(request_prompt):
                """Dispatch the artifact request; a deadline cut here is the evaluator's.

                Without this, a request the evaluator's own case deadline killed left
                the case at exit 124 (lower-agent infrastructure) or, with a settled
                broker row, at "Candidate product produced no valid model-authored
                agent artifact".  Neither is true: the budget ran out.
                """
                try:
                    return model_json(lower, endpoint, request_prompt, case_id, "artifact", remaining,
                                      capture, evaluation_scope=evaluation_scope)
                except (TimeoutError, socket.timeout) as exception:
                    try:
                        left = remaining()
                    except TimeoutError:
                        left = 0.0
                    if left > MODEL_ARTIFACT_MINIMUM_SECONDS:
                        raise
                    raise CaseBudgetReserveReached(
                        "final-artifact request cut by the case deadline with "
                        f"{round(left, 1)}s left: {type(exception).__name__}") from None

            authored, response_digest = ask_artifact(prompt)
            artifact = authored.get("artifact", authored)
            defect = artifact_mode_defect(artifact)
            if defect is not None and ARTIFACT_REASK_TURNS >= 1:
                # One explicit artifact-mode re-ask.  This is a NEW logical request
                # with a different prompt, never a resample of the one just answered
                # (broker protocol: inner_retries 0), exactly like the format repair.
                try:
                    reask_remaining = remaining()
                except TimeoutError:
                    reask_remaining = 0.0
                record = {"schema_version": ARTIFACT_REASK_SCHEMA, "case_id": case_id, "attempted": False,
                          "defect": defect, "remaining_seconds": round(reask_remaining, 1),
                          "minimum_seconds": ARTIFACT_REASK_MINIMUM_SECONDS,
                          "first_response_sha256": response_digest, "repaired": False}
                if reask_remaining >= ARTIFACT_REASK_MINIMUM_SECONDS:
                    previous_text = capture.read_text(encoding="utf-8")
                    record["moved_aside"] = stash_artifact_capture(capture, "action_shaped")
                    record["attempted"] = True
                    stage = "artifact_model_reask"; phase(stage, defect=defect)
                    authored, response_digest = ask_artifact(artifact_mode_prompt(prompt, defect, previous_text))
                    artifact = authored.get("artifact", authored)
                    record["second_defect"] = artifact_mode_defect(artifact)
                    record["repaired"] = record["second_defect"] is None
                    record["response_sha256"] = response_digest
                write(output / "artifact_reask.json", record)
            stage = "artifact_validation"; phase(stage, response_sha256=response_digest)
            lower.validate_trajectory_binding(output, case_id, artifact, trusted_origin=completed_origin(lower, output, case_id, artifact))
            write(output / "agent_result.json", artifact)
            phase("complete", response_sha256=response_digest)
        else:
            phase("provider_free_diagnostic_complete", model_calls=0, score=None)
    except CaseBudgetReserveReached as exception:
        # Not 124/125: the evaluator stopped at its own budget guard, the ledger holds
        # only settled rows, and the artifact is absent.  The controller re-attributes
        # this to the evaluator (evaluator_budget_truncation), not to the Candidate.
        code, error = 1, f"case_budget_reserve_reached {exception}"
    except CandidateResponseTruncated as exception:
        code, error = 1, f"candidate_response_truncated {exception}"
    except (TimeoutError, socket.timeout) as exception:
        code, error = 124, f"{stage}: {type(exception).__name__}: {exception}"
    except (urllib.error.URLError, urllib.error.HTTPError) as exception:
        if _broker_error_type(exception) == "CaseDeadlineExceeded":
            # package 120 (2026-09-23): the broker reporting the case's own deadline is the
            # evaluator's clock, not a provider failure -- book it as the case timeout.
            code, error = 124, f"{stage}: CaseDeadlineExceeded (reported by the candidate broker)"
        else:
            code, error = 125, f"agent_server_provider_failure {type(exception).__name__}: {exception}"
    except Exception as exception:
        code, error = (125 if stage in {"preflight", "model_request", "artifact_model_request", "model_choice_validation", "artifact_validation"} else 1), f"{stage}: {type(exception).__name__}: {exception}"
    finally:
        try:
            if product_socket.exists(): request(product_socket, "/shutdown", {}, 2)
        except Exception: pass
        try:
            cleanup = cleanup_container(name)
        except Exception as cleanup_error:
            cleanup = {"container_name": name, "container_absent": False, "error": type(cleanup_error).__name__}
        for process in (product_process, world_process):
            if process and process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
        cleanup.update(world_process_stopped=world_process is None or world_process.poll() is not None,
                       product_process_stopped=product_process is None or product_process.poll() is not None, ipc_root=str(ipc_root))
        if cleanup["container_absent"] and cleanup["world_process_stopped"] and cleanup["product_process_stopped"]:
            shutil.rmtree(ipc_root)
            cleanup["owned_temporary_ipc_removed"] = True
        else:
            code, error = 125, error + "; isolated runtime cleanup unconfirmed"
        write(output / "runtime_cleanup.json", cleanup)
        for handle in logs: handle.close()
    return subprocess.CompletedProcess(command, code, "", error)


def main_worker(lower):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--broker-endpoint", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node-modules", type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--provider-free-actions", type=Path)
    args = parser.parse_args()
    if not 0 < args.timeout <= 600: raise ValueError("case total budget must be within 600 seconds")
    candidate, case, output = args.repository.resolve(), args.case.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any((output / name).exists() for name in ("launcher_result.json", "trajectory.json", "agent_result.json")):
        raise ValueError("refusing to overwrite case evidence")
    started = time.monotonic()
    digest = lower.tree_digest(candidate)
    summary = {"schema_version": "agentswe-openhands-lower-launch/v3", "case_id": case.name,
        "candidate_product": lower.PRODUCT_NAME, "candidate_source_digest": digest,
        "model_protocol": {"model": lower.MODEL, "reasoning_effort": lower.EFFORT},
        "broker_endpoint_is_evaluator_owned": True, "credential_seen_by_candidate": "broker-only-placeholder",
        "evaluator_source_mounted": False, "private_oracle_mounted": False,
        "candidate_network": "none", "trusted_model_and_world_outside_candidate": True}
    try:
        summary["product_attestation"] = lower.product_identity(candidate)
        modules = args.node_modules or (Path(os.environ["OPENHANDS_NODE_MODULES"]) if os.environ.get("OPENHANDS_NODE_MODULES") else None)
        if modules is None: raise FileNotFoundError("prepared --node-modules required")
        repo = lower.materialize_runtime_repository(candidate, output, modules.resolve())
        task_path = case / "natural_task.md" if (case / "natural_task.md").is_file() else case / "input.md"
        fixture_path = case / "assets/fixtures.json"
        if not fixture_path.is_file(): fixture_path = case / "assets/scenario.json"
        fixture = json.loads(fixture_path.read_text()) if fixture_path.is_file() else {}
        actions = json.loads(args.provider_free_actions.read_text()) if args.provider_free_actions else None
        if actions is not None and (not isinstance(actions, list) or len(actions) > 12): raise ValueError("diagnostic actions must be a list of at most twelve")
        process = run_isolated(lower, repo=repo, case_id=case.name, task=task_path.read_text(), nonce=secrets.token_hex(16),
            fixture=fixture, output=output, endpoint=args.broker_endpoint, timeout=max(1, int(args.timeout - (time.monotonic() - started)) - WORKER_EXIT_MARGIN_SECONDS), diagnostic_actions=actions)
        artifact_path = output / "agent_result.json"
        artifact = json.loads(artifact_path.read_text()) if artifact_path.is_file() else None
        binding = lower.validate_trajectory_binding(output, case.name, artifact, trusted_origin=completed_origin(lower, output, case.name, artifact)) if artifact else None
        phase_path = output / "execution_phase.json"
        execution_phase = json.loads(phase_path.read_text()) if phase_path.is_file() else {}
        classification = lower.classify_process(process.returncode, process.stderr, execution_phase, artifact, binding)
        summary.update(exit_code=process.returncode, classification=classification, execution_phase=execution_phase,
            stderr_tail=lower.redact_runtime_text(process.stderr, candidate, repo, output), stdout_tail="",
            real_execution=actions is None and (output / "trajectory.json").is_file(),
            task_source=str(task_path), task_sha256=hashlib.sha256(task_path.read_bytes()).hexdigest(),
            fixture_sha256=hashlib.sha256(fixture_path.read_bytes()).hexdigest() if fixture_path.is_file() else None,
            runtime_seconds=round(time.monotonic() - started, 3), immutable_candidate_unchanged=lower.tree_digest(candidate) == digest)
        for name, field in (("case_world.json", "case_world_sha256"), ("trajectory.json", "trajectory_sha256"), ("agent_result.json", "agent_artifact_sha256"), ("final_product_state.json", "final_product_state_sha256"), ("model_final_response.txt", "model_final_response_sha256"), ("environment_preflight.json", "environment_preflight_sha256")):
            path = output / name; summary[field] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        summary["format_repair_requests"] = len(list(output.glob("*.format_repair.json")))
        final_repair = format_repair_record(output / "model_final_response.txt")
        if final_repair: summary["format_repair"] = final_repair
        final_trailing = artifact_trailing_record(output / "model_final_response.txt")
        if final_trailing: summary["artifact_response_trailing_bytes"] = final_trailing
        if binding: summary.update(trajectory_binding=binding, agent_result="agent_result.json")
        if actions is not None:
            summary.update(classification="provider_free_product_diagnostic", formal_result_publishable=False, acceptance_result_publishable=False,
                model_calls=0, real_execution=False, actual_product_diagnostic=True, score=None)
    except Exception as error:
        summary.update(exit_code=125, classification="lower_agent_infrastructure_failure", real_execution=False, stderr_tail=lower.redact_runtime_text(f"{type(error).__name__}: {error}", candidate, output))
    lower.write_launcher(output, summary)
    print(json.dumps(summary, indent=2))
    return 0 if summary["exit_code"] == 0 else 1


def main(lower):
    """Supervise one total case budget; all workload including Docker is in it."""
    if "--resource-worker" in sys.argv:
        sys.argv.remove("--resource-worker")
        unit = _ambient_aggregate()
        if unit is None: raise RuntimeError("resource-worker requires a verified evaluator-owned parent")
        verify_aggregate({"unit": unit, "cgroup": "/" + unit, "memory_bytes": MEMORY_BYTES})
        return main_worker(lower)
    if "--help" in sys.argv or "-h" in sys.argv: return main_worker(lower)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    args, _ = parser.parse_known_args()
    if not 0 < args.timeout <= 600: raise ValueError("case total budget must be within 600 seconds")
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    if any((output / name).exists() for name in ("launcher_result.json", "trajectory.json", "agent_result.json", "owned_resources")):
        raise ValueError("refusing to overwrite or restart case evidence")
    cleanup_reserve = min(30, max(1, int(args.timeout * .1)))
    work_budget = max(1, args.timeout - cleanup_reserve)
    command = ["/usr/bin/python3", "-E", "-s", "-B", str(Path(lower.__file__).resolve()), "--resource-worker", *sys.argv[1:], "--timeout", str(work_budget)]
    process, resource = run_owned(command, cwd=Path.cwd(), env=lower.sanitized_environment(), output=output / "owned_resources", timeout=work_budget, memory_bytes=MEMORY_BYTES)
    result_path = output / "launcher_result.json"
    try: summary = json.loads(result_path.read_text())
    except (OSError, ValueError): summary = {"schema_version": "agentswe-openhands-lower-launch/v3", "exit_code": process.returncode, "classification": "lower_agent_infrastructure_failure", "stderr_tail": lower.redact_runtime_text(process.stderr, output)}
    if process.returncode and not summary.get("exit_code"):
        summary.update(exit_code=process.returncode, classification="lower_agent_infrastructure_failure")
    if resource.get("elapsed_seconds", float("inf")) > args.timeout:
        summary.update(exit_code=125, classification="lower_agent_infrastructure_failure", total_deadline_exceeded=True)
    summary.update(aggregate_resource_enforcement={"memory_bytes": MEMORY_BYTES, "total_timeout_seconds": args.timeout,
        "work_timeout_seconds": work_budget, "cleanup_reserve_seconds": cleanup_reserve, "elapsed_seconds_including_cleanup": resource.get("elapsed_seconds"),
        "scope": "source hashing, dependency materialization, world, model loop, product, artifact and observer share the verified parent; supervisor remains outside",
        "attestation_path": str(output / "owned_resources/resource-attestation.json"), "attestation_sha256": hashlib.sha256((output / "owned_resources/resource-attestation.json").read_bytes()).hexdigest(),
        "aggregate_parent": resource["aggregate_parent"], "aggregate_cleanup": resource.get("aggregate_cleanup")})
    lower.write_launcher(output, summary)
    print(json.dumps(summary, indent=2))
    return 0 if process.returncode == 0 and summary.get("exit_code") == 0 else 1
