"""Admission of evaluator evidence anchored by independently obtained SHA/binding."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath

PROFILE = "single-dev-two-round-hidden-smoke-v1"
FILES = ("solution.patch", "edit_report.json", "run_report.json")
ROLES = ("builder", "public_lower", "hidden_lower", "result_judge", "code_judge")
# The Code axis was retired on 2026-09-19 (Result-only evaluation). A bundle records
# the retirement explicitly: judges.code carries state "skipped_by_policy" and
# usage.code_judge carries all-zero accounting under the same policy id.
CODE_AXIS_POLICY_ID = "edit-code-axis-retired-2026-09-19"
CODE_SKIPPED = "skipped_by_policy"
# Recovered-transport tolerance. An upstream transport failure inside an accepted public dev round, which
# the lower agent recovered from by issuing a new logical request, is tolerated as
# infrastructure noise: it stays a failure whose usage is unknown, contributes no known
# tokens, and is listed by identity in the role's usage receipt. It is the only case in which
# a request may carry usage_known False, and only for the roles named here.
RECOVERED_TRANSPORT_POLICY_ID = "edit-recovered-transport-2026-09-19"
RECOVERED_TRANSPORT_ROLES = ("public_lower",)
# Case-deadline tolerance. Budget exhaustion is a Candidate outcome (candidate_timeout, a hard
# zero), not an infrastructure fault, so a lower ledger whose only failures are the
# evaluator's own case-deadline kills is admissible. Such a request is carried in exactly
# the recovered-transport unknown-usage shape -- a named failure with zero known tokens -- and additionally
# marked `deadline_killed`, which the normalizer only produces from a ledger that declares
# the kill itself. It is the one tolerance that reaches the hidden lower role, because the
# hidden smoke is where a case can spend its whole budget. Nothing else moves: the role
# still needs a successful accounted call, and both judge roles stay strict.
DEADLINE_POLICY_ID = "edit-case-deadline-candidate-outcome-2026-09-21"
DEADLINE_ERROR_PREFIX = "deadline:"
DEADLINE_KILL_ROLES = ("public_lower", "hidden_lower")


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_digest(root):
    """Match agentloop.protocol.tree_digest, including directory/link entries."""
    root = Path(root)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            kind, payload = b"L", os.readlink(path).encode()
        elif path.is_file():
            kind, payload = b"F", path.read_bytes()
        elif path.is_dir():
            kind, payload = b"D", b""
        else:
            raise ValueError("special file in tree")
        digest.update(len(name).to_bytes(8, "big")); digest.update(name)
        digest.update(kind); digest.update(len(payload).to_bytes(8, "big")); digest.update(payload)
    return digest.hexdigest()


def is_hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _native_segment_ledger(require, artifact, native, session, count):
    """Validate the evaluator's exported Builder segment ledger.

    Multi-segment native evidence is admissible only with this file: it is
    written by the evaluator in the run root, outside every Builder mount, and
    it is re-validated here against the exported logs rather than trusted.
    """
    reference = native.get("builder_segments")
    require(isinstance(reference, dict), "native segment ledger missing")
    value = artifact(reference)
    require(isinstance(value, dict) and
            value.get("schema_version") == "agentswe-builder-segments/v1",
            "unknown native segment ledger")
    cap = value.get("resume_cap")
    require(type(cap) is int and 0 <= cap <= 2, "invalid native resume cap")
    rows = value.get("segments")
    require(isinstance(rows, list) and len(rows) == count and len(rows) <= cap + 1,
            "native segment ledger disagrees with the exported logs")
    deadline = value.get("builder_deadline_epoch")
    require(isinstance(deadline, (int, float)) and not isinstance(deadline, bool),
            "native segment ledger has no budget deadline")
    for index, row in enumerate(rows, 1):
        require(isinstance(row, dict) and row.get("segment_index") == index,
                "invalid native segment ledger")
        started = row.get("started_at_epoch")
        require(isinstance(started, (int, float)) and not isinstance(started, bool)
                and started <= deadline, "native segment started after the budget deadline")
        require(row.get("session_id") == session, "native segment is not this session")
        if index > 1:
            require(row.get("resume_of_session_id") == rows[0].get("session_id"),
                    "native segment does not resume the first session")
    return True


def unrecovered_native_errors(events):
    """Fatal native events: turn.failed, or an error that is not a recovered transport reconnect.
    A codex-CLI "Reconnecting..." error is tolerated only because the turn accounting below
    still requires every started turn to complete (2026-09-19)."""
    return [v for v in events if v.get("type") == "turn.failed" or
            (v.get("type") == "error" and not str(v.get("message", "")).startswith("Reconnecting..."))]


def checked_path(root, relative, *, directory=False):
    """Reject lexical traversal and symlink components before resolve/read."""
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("invalid artifact path")
    if PurePosixPath(relative).is_absolute() or any(p in ("", ".", "..") for p in relative.split("/")):
        raise ValueError("artifact path escape")
    path = Path(root)
    for part in relative.split("/"):
        path = path / part
        if stat.S_ISLNK(path.lstat().st_mode):
            raise ValueError("symlink artifact component")
    mode = path.lstat().st_mode
    if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
        raise ValueError("artifact is not expected regular file/directory")
    return path


def _root(value):
    if value is None:
        raise ValueError("external bundle root required")
    root = Path(os.path.abspath(value))
    for path in (root, *root.parents):
        if path.is_symlink():
            raise ValueError("bundle root contains symlink")
    if not root.is_dir():
        raise ValueError("external bundle root unavailable")
    return root


def _json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("nonfinite JSON")
    return json.loads(path.read_text(), object_pairs_hook=pairs, parse_constant=constant)


def validate_evidence(e, bundle_root=None, expected_manifest_sha256=None, trusted_current_binding=None, *, judge_output_validators=None, broker_record_normalizers=None, notes=None):
    def require(condition, message):
        if not condition:
            raise ValueError(message)
    try:
        root = _root(bundle_root)
        require(is_hash(expected_manifest_sha256), "external manifest SHA required")
        manifest = checked_path(root, "manifest.json")
        require(sha256_file(manifest) == expected_manifest_sha256, "manifest anchor mismatch")
        require(isinstance(e, dict) and json.dumps(_json(manifest), sort_keys=True, allow_nan=False) ==
                json.dumps(e, sort_keys=True, allow_nan=False), "evidence differs from anchored manifest")
        binding = trusted_current_binding
        require(isinstance(binding, dict) and all(is_hash(binding.get(k)) for k in
                ("source_digest", "contract_digest", "registry_digest")) and
                isinstance(binding.get("task"), str) and bool(binding["task"]), "external current binding malformed")
        require(e.get("current_binding") == binding, "current source/contract/registry/task binding mismatch")
        require(e.get("profile") == PROFILE, "profile mismatch")
        run_id = e["run_id"]
        require(isinstance(run_id, str) and bool(run_id), "run ID required")

        def artifact(ref, *, parsed=True):
            require(isinstance(ref, dict) and is_hash(ref.get("sha256")), "artifact hash required")
            path = checked_path(root, ref["path"])
            require(sha256_file(path) == ref["sha256"], "artifact hash mismatch: " + ref["path"])
            return _json(path) if parsed else path

        def receipt(ref):
            value = artifact(ref)
            require(isinstance(value, dict) and value.get("run_id") == run_id and
                    value.get("owner") == "evaluator", "receipt run/owner mismatch")
            return value

        def directory_artifact(ref):
            require(isinstance(ref, dict) and is_hash(ref.get("tree_digest")), "directory artifact digest required")
            path = checked_path(root, ref["path"], directory=True)
            for child in path.rglob("*"):
                mode = child.lstat().st_mode
                require(stat.S_ISREG(mode) or stat.S_ISDIR(mode), "directory artifact symlink/special file")
            require(tree_digest(path) == ref["tree_digest"], "directory artifact digest mismatch")
            return path

        artifact_access = {"read_json": artifact, "read_bytes": lambda ref: artifact(ref, parsed=False).read_bytes(),
                           "directory": directory_artifact, "run_id": run_id}

        def issued_feedback_digest(ref):
            path = artifact(ref, parsed=False)
            algorithm = ref.get("digest_algorithm")
            if algorithm == "sha256-bytes-v1":
                return sha256_file(path)
            require(algorithm in ("canonical-json-without-feedback-digest-v1", "canonical-json-without-feedback-digest-no-newline-v1"), "feedback digest algorithm required")
            payload = _json(path)
            require(isinstance(payload, dict), "feedback payload must be object")
            claimed = payload.pop("feedback_digest", None)
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            if algorithm == "canonical-json-without-feedback-digest-v1":
                canonical += b"\n"
            actual = hashlib.sha256(canonical).hexdigest()
            require(claimed == actual, "feedback canonical digest mismatch")
            return actual

        native = receipt(e["builder_native"])
        session = native["builder_session_id"]
        require(isinstance(session, str) and bool(session), "native session missing")
        require(native.get("model") == "deepseek-flash" and native.get("effort") == "max" and
                native.get("state") == "terminal" and native.get("thread_ids") == [session], "native session/model/state invalid")
        # A Builder session cut by an infrastructure failure and resumed with
        # `codex exec resume <id>` is exported as several logs of ONE session.
        # Multi-segment is admissible only when the evaluator's own segment
        # ledger is exported beside them; with one log every rule below is the
        # pre-resume rule, unchanged and in the same order.
        logs = native.get("native_logs") or [native["native_log"]]
        require(isinstance(logs, list) and logs and logs[-1] == native["native_log"],
                "native log set invalid")
        if len(logs) == 1:
            data = artifact(native["native_log"], parsed=False).read_bytes()
            require(not data or data.endswith(b"\n"), "native log has incomplete final line")
            # Pinned Harbor codex.txt mixes stderr/rendered output with native
            # compact frames. Match the source-bound native_builder_evidence rule.
            events = [json.loads(line) for line in data.splitlines() if line.startswith(b'{"type":')]
            require(all(isinstance(v, dict) for v in events), "native event not object")
            require([v.get("thread_id") for v in events if v.get("type") == "thread.started"] == [session],
                    "native log does not prove one uninterrupted thread")
            turns = [v.get("usage") for v in events if v.get("type") == "turn.completed"]
            require(turns and sum(v.get("type") == "turn.started" for v in events) == len(turns) and
                    not unrecovered_native_errors(events), "native turns incomplete/error")
        else:
            ledger = _native_segment_ledger(require, artifact, native, session, len(logs))
            turns, events = [], []
            for index, reference in enumerate(logs, 1):
                data = artifact(reference, parsed=False).read_bytes()
                require(not data or data.endswith(b"\n"), "native log has incomplete final line")
                rows = [json.loads(line) for line in data.splitlines() if line.startswith(b'{"type":')]
                require(all(isinstance(v, dict) for v in rows), "native event not object")
                require([v.get("thread_id") for v in rows if v.get("type") == "thread.started"] == [session],
                        "native log does not prove one uninterrupted thread")
                require(sum(v.get("type") == "turn.started" for v in rows) == 1,
                        "native segment does not prove one continuous invocation")
                completed = [v.get("usage") for v in rows if v.get("type") == "turn.completed"]
                if index < len(logs):
                    # A cut segment: started, never completed, never a thread failure.
                    require(not completed and not any(v.get("type") == "thread.failed" for v in rows),
                            "native cut segment is not an interrupted turn")
                else:
                    require(completed and not unrecovered_native_errors(rows),
                            "native turns incomplete/error")
                turns.extend(completed)
                events.extend(rows)
            require(turns and ledger, "native turns incomplete/error")
        require(all(isinstance(v, dict) and all(type(v.get(k)) is int and v[k] >= 0 for k in
                ("input_tokens", "cached_input_tokens", "output_tokens")) and v["cached_input_tokens"] <= v["input_tokens"]
                for v in turns), "native turn usage malformed")
        native_tokens = sum(v["input_tokens"] + v["output_tokens"] for v in turns)
        require(native_tokens > 0, "native usage absent")
        rounds = e["public_rounds"]
        require(isinstance(rounds, list) and len(rounds) == 2, "exactly two public rounds required")
        previous = feedback_digest = None
        for number, row in enumerate(rounds, 1):
            require(row.get("case") == "dev_001" and type(row.get("submission_number")) is int and row["submission_number"] == number and
                    row.get("builder_session_id") == session, "round/session/case mismatch")
            directory = checked_path(root, row["submission_dir"], directory=True)
            require(set(p.name for p in directory.iterdir()) == set(FILES), "submission must have exactly three files")
            hashes = {name: sha256_file(checked_path(root, row["submission_dir"] + "/" + name)) for name in FILES}
            require(hashes == row["submission_sha256"], "submission SHA mismatch")
            candidate = tree_digest(directory)
            require(candidate == row.get("candidate_digest") and candidate != previous, "candidate identity/distinctness mismatch")
            report = _json(directory / "run_report.json")
            metadata = {"builder_session_id": session, "submission_number": number,
                        "revision_of_candidate_digest": previous, "feedback_digest": feedback_digest}
            require(all(report.get(k) == v and k in report for k, v in metadata.items()), "submission metadata mismatch")
            execution = receipt(row["execution"])
            require(all(execution.get(k) == v for k, v in {**metadata, "candidate_digest": candidate,
                    "case": "dev_001", "state": "terminal", "classification": "execution_valid",
                    "current_binding": binding}.items()), "public execution binding/classification mismatch")
            require(execution.get("transport") == "complete" and type(execution.get("build_exit_code")) is int and
                    execution["build_exit_code"] == 0, "public transport/build invalid")
            materialized = execution.get("materialized_source_digest_before_build")
            require(is_hash(materialized) and materialized == execution.get("materialized_source_digest_after_build") ==
                    row.get("materialized_source_digest"), "materialized source drift")
            require(is_hash(row.get("materialized_repository_digest")) and
                    execution.get("materialized_repository_digest") == row["materialized_repository_digest"],
                    "materialized repository digest missing/mismatch")
            result = receipt(execution["result"])
            require(result.get("candidate_digest") == candidate and result.get("case") == "dev_001" and
                    result.get("state") == "terminal" and result.get("classification") == "execution_valid", "public result mismatch")
            feedback = artifact(row["feedback"], parsed=False)
            require(execution.get("feedback_sha256") == sha256_file(feedback), "issued feedback mismatch")
            if number == 2:
                require(hashes["solution.patch"] != rounds[0]["submission_sha256"]["solution.patch"] and
                        materialized != rounds[0]["materialized_source_digest"], "revision has no product change")
                consumed = artifact(execution["consumed_feedback"], parsed=False)
                require(consumed.read_bytes() == artifact(rounds[0]["feedback"], parsed=False).read_bytes(), "consumed feedback bytes mismatch")
                require(row.get("revision_of_candidate_digest") == previous and row.get("feedback_digest") == feedback_digest,
                        "exact feedback revision mismatch")
                edit = _json(directory / "edit_report.json")
                require(isinstance(edit.get("feedback_response"), str) and bool(edit["feedback_response"].strip()), "revision explanation required")
            previous, feedback_digest = candidate, issued_feedback_digest(row["feedback"])
        freeze = receipt(e["freeze"])
        require(all(freeze.get(k) == v for k, v in {"delivery_candidate_digest": previous,
                "candidate_digest": rounds[-1]["materialized_repository_digest"], "builder_session_id": session,
                "submission_sha256": hashes, "current_binding": binding}.items()), "freeze binding mismatch")
        hidden = receipt(e["hidden_smoke"])
        require(all(hidden.get(k) == v for k, v in {"case": "test_001", "state": "terminal",
                "classification": "execution_valid", "transport": "complete", "readiness_only": True,
                "builder_access": False, "freeze_sha256": e["freeze"]["sha256"]}.items()), "hidden smoke invalid")
        hidden_result = receipt(hidden["result"])
        require(hidden_result.get("case") == "test_001" and hidden_result.get("state") == "terminal" and
                hidden_result.get("candidate_digest") == freeze["candidate_digest"], "hidden result binding mismatch")
        identities = {session}
        for role in ("result", "code"):
            judge = receipt(e["judges"][role])
            if role == "code" and judge.get("state") == CODE_SKIPPED:
                require(all(judge.get(k) == v for k, v in {"role": "code", "formal": False, "judge_session_id": None,
                        "request_id": None, "freeze_sha256": e["freeze"]["sha256"]}.items())
                        and isinstance(judge.get("policy"), dict) and judge["policy"].get("id") == CODE_AXIS_POLICY_ID
                        and "input" not in judge and "output" not in judge, "skipped Code judge receipt malformed")
                continue
            identity = judge.get("judge_session_id")
            require(isinstance(identity, str) and bool(identity) and identity not in identities, "judge identity not independent")
            identities.add(identity)
            require(all(judge.get(k) == v for k, v in {"role": role, "model": "deepseek-flash", "effort": "max",
                    "state": "terminal", "formal": False, "freeze_sha256": e["freeze"]["sha256"]}.items()), "judge binding/model mismatch")
            input_value = artifact(judge["input"])
            require(input_value.get("freeze_sha256") == e["freeze"]["sha256"] and input_value.get("role") == role, "judge input binding mismatch")
            output = artifact(judge["output"])
            require(isinstance(output, dict) and output.get("role") == role and
                    output.get("judge_session_id") == identity and isinstance(output.get("payload"), dict),
                    "judge output identity/payload mismatch")
            require(isinstance(judge_output_validators, dict) and callable(judge_output_validators.get(role)),
                    "external trusted judge output validator required")
            validation_errors = judge_output_validators[role](output["payload"], input_value)
            require(validation_errors == [], "judge output schema invalid: " + str(validation_errors))
        require(set(e["usage"]) == set(ROLES), "per-role usage incomplete")
        request_ids = set()
        require(isinstance(broker_record_normalizers, dict), "external broker record normalizers required")
        role_requests = {}
        request_state, recovered_requests = {}, set()
        deadline_requests, deadline_rows_accepted = set(), {}
        for role in ROLES:
            ledger = receipt(e["usage"][role])
            require(ledger.get("role") == role, "usage role mismatch")
            if role == "builder":
                require(all(ledger.get(k) == v for k, v in {
                    "transport": "native_codex_direct", "builder_broker_started": False,
                    "native_completed_turns": len(turns), "native_reported_usage": turns,
                    "actual_upstream_requests": None, "complete_provider_billing_claimed": False,
                    "native_usage_complete": True, "known_tokens": native_tokens,
                    "builder_session_id": session}.items()) and "actual_upstream_requests" in ledger,
                    "native Builder usage accounting mismatch")
                require(ledger.get("native_log") == native["native_log"] and
                        not any(k in ledger for k in ("requests", "calls", "actual_upstream_attempts")),
                        "native Builder must not fabricate broker accounting")
                continue
            keys = ("calls", "actual_upstream_attempts", "successes", "failures", "known_tokens", "unknown_usage", "in_flight")
            require(all(type(ledger.get(k)) is int and ledger[k] >= 0 for k in keys), "usage explicit nonnegative accounting required")
            if role == "code_judge" and ledger.get("skipped_by_policy") is True:
                require(all(ledger[k] == 0 for k in keys) and ledger.get("requests") == [] and
                        isinstance(ledger.get("policy"), dict) and ledger["policy"].get("id") == CODE_AXIS_POLICY_ID,
                        "skipped Code judge usage malformed")
                role_requests[role] = set()
                continue
            requests = ledger["requests"]
            require(isinstance(requests, list) and len(requests) == ledger["calls"] and requests and
                    all(isinstance(r, dict) for r in requests), "actual role requests required")
            # Tolerated transport failures are the only unknown usage admitted, only for
            # the roles above, and the receipt has to name each one.
            recovered = [r for r in requests if r.get("recovered_transport") is not None]
            require(all(r.get("recovered_transport") is True for r in recovered), "recovered transport flag malformed")
            # A case-deadline kill is one of those rows, additionally marked and named
            # with the ledger's own `deadline:` reason. It is admitted for the lower roles;
            # an ordinary recovered transport failure keeps the public-only rule.
            killed = [r for r in recovered if r.get("deadline_killed") is not None]
            require(all(r.get("deadline_killed") is True and isinstance(r.get("error"), str)
                        and r["error"].startswith(DEADLINE_ERROR_PREFIX) for r in killed),
                    "case-deadline kill flag/reason malformed")
            require(not killed or role in DEADLINE_KILL_ROLES,
                    "case-deadline kill not tolerated for role: " + role)
            require(not [r for r in recovered if r.get("deadline_killed") is None]
                    or role in RECOVERED_TRANSPORT_ROLES,
                    "recovered transport failure not tolerated for role: " + role)
            deadline_rows_accepted[role] = len(killed)
            require(ledger["in_flight"] == 0 and ledger["unknown_usage"] == len(recovered), "unknown/inflight usage")
            require(ledger.get("recovered_transport_failures", []) ==
                    [{"request_id": r.get("request_id"), "error": r.get("error")} for r in recovered],
                    "recovered transport failures not recorded in the usage receipt")
            require(ledger["known_tokens"] > 0 and ledger["successes"] > 0, "role lacks successful accounted provider use")
            if role.endswith("_judge"):
                require(ledger["calls"] == ledger["successes"] == 1 and ledger["failures"] == 0,
                        "judge smoke requires one successful logical request")
            attempts = successes = failures = tokens = 0
            role_requests[role] = set()
            for request in requests:
                rid = request.get("request_id")
                require(isinstance(rid, str) and rid and rid not in request_ids, "request reused/missing")
                request_ids.add(rid)
                role_requests[role].add(rid)
                if request.get("recovered_transport") is True:
                    require(request.get("state") == "failure" and request.get("usage_known") is False and
                            request.get("known_tokens") == 0 and isinstance(request.get("error"), str) and
                            bool(request["error"]), "recovered transport request malformed")
                    recovered_requests.add(rid)
                    if request.get("deadline_killed") is not None:
                        deadline_requests.add(rid)
                else:
                    require(request.get("state") in ("success", "failure") and request.get("usage_known") is True, "request outcome unknown")
                request_state[rid] = request.get("state")
                require(all(type(request.get(k)) is int and request[k] >= 0 for k in ("upstream_attempts", "known_tokens")), "request accounting malformed")
                require(request["upstream_attempts"] >= 1, "request lacks upstream attempt")
                raw = artifact(request["provider_record"])
                normalizer = broker_record_normalizers[role]
                if callable(getattr(normalizer, "normalize_with_artifacts", None)):
                    normalized = normalizer.normalize_with_artifacts(raw, rid, artifact_access)
                else:
                    normalized = normalizer(raw, rid)
                fields = ("request_id", "state", "upstream_attempts", "known_tokens", "usage_known")
                if request.get("recovered_transport") is True:
                    fields += ("recovered_transport", "error")
                    if request.get("deadline_killed") is not None:
                        fields += ("deadline_killed",)
                require(isinstance(normalized, dict) and
                        ("recovered_transport" in normalized) == (request.get("recovered_transport") is True) and
                        normalized == {k: request.get(k) for k in fields}, "provider usage evidence mismatch")
                attempts += request["upstream_attempts"]; tokens += request["known_tokens"]
                successes += request["state"] == "success"; failures += request["state"] == "failure"
            require((attempts, successes, failures, tokens) == tuple(ledger[k] for k in
                    ("actual_upstream_attempts", "successes", "failures", "known_tokens")), "usage aggregate mismatch")
        public_request_ids = set()
        for row in rounds:
            execution = receipt(row["execution"])
            ids = execution.get("request_ids")
            require(isinstance(ids, list) and ids and len(ids) == len(set(ids)) and
                    set(ids).issubset(role_requests["public_lower"]) and not set(ids) & public_request_ids,
                    "public round usage request binding missing/reused")
            # The fault is only noise if its own accepted round still produced a
            # completed request. A round that ended on the failure is not a recovered one.
            # A case-deadline kill is terminal for its case by construction -- the
            # evaluator ended it -- so it is the one tolerated row that needs no later
            # success in its own round. Every other tolerated row still does.
            require(all(any(request_state.get(other) == "success" for other in ids)
                        for rid in ids if rid in recovered_requests and rid not in deadline_requests),
                    "recovered transport failure has no completed request in its round")
            public_request_ids.update(ids)
        require(public_request_ids == role_requests["public_lower"], "public usage has unattributed requests")
        for role in ("result", "code"):
            judge = receipt(e["judges"][role])
            if role == "code" and judge.get("state") == CODE_SKIPPED:
                continue
            require(judge.get("request_id") in role_requests[role + "_judge"], "judge usage request binding missing")
        hidden = receipt(e["hidden_smoke"])
        require(hidden.get("request_ids") and set(hidden["request_ids"]).issubset(role_requests["hidden_lower"]), "hidden usage request binding missing")
        cleanup = receipt(e["cleanup"])
        before, after = receipt(cleanup["before"]), receipt(cleanup["after"])
        require(all(cleanup.get(k) == "terminal" for k in ("unit_state", "harbor_state", "builder_state")), "cleanup while run live")
        owned = cleanup["owned_resources"]
        require(isinstance(owned, list) and owned and len(set(owned)) == len(owned), "cleanup ownership malformed")
        resources = before["resources"]
        require(all(isinstance(resources.get(r), dict) and resources[r].get("run_id") == run_id and
                    resources[r].get("state") == "terminal" for r in owned), "cleanup ownership/terminal evidence missing")
        require(all(r not in after["resources"] for r in owned), "owned resource remains")
        require({k: v for k, v in resources.items() if k not in owned} == after["resources"], "unrelated resources changed")
        require(set(cleanup["stats"]) == set(owned), "owned resource stats incomplete")
        for resource in owned:
            require(receipt(cleanup["stats"][resource]).get("resource_id") == resource, "resource stats mismatch")
        # Report the tolerance that was exercised, so the admission can record it.
        # Set only after every check above passed, and only when the caller asked for it.
        if isinstance(notes, dict):
            notes["deadline_policy_id"] = DEADLINE_POLICY_ID
            notes["deadline_rows_accepted"] = dict(deadline_rows_accepted)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return False, [str(exc)]
    return True, []


def load_and_validate(path, bundle_root=None, expected_manifest_sha256=None, trusted_current_binding=None, *, judge_output_validators=None, broker_record_normalizers=None, notes=None):
    try:
        return validate_evidence(_json(Path(path)), bundle_root, expected_manifest_sha256, trusted_current_binding,
                                 judge_output_validators=judge_output_validators, broker_record_normalizers=broker_record_normalizers,
                                 notes=notes)
    except (OSError, ValueError, TypeError) as exc:
        return False, [f"cannot read evidence: {exc}"]
