"""Read original per-request receipt trees and instrumented Claude broker stats.

Receipt indexes are evaluator metadata, never raw provider records. Every
referenced original byte is checked through the validator-owned bundle reader.

The recovered-transport tolerance deliberately does not reach these three tasks, and they keep the strict
refusal above:

  * aider/codex receipt directories carry a durable identity for a failed request (the
    directory name) but no anchored order, and `intent.json` has no sequence number, so
    "the lower agent continued after this fault" cannot be proven from the tree. A
    request directory holding failure.json or recovered_interruption.json still refuses.
  * Claude broker rows have no identity for a failed request -- `response_id` is None on
    the failure path -- so a tolerated row could not be named in the usage receipt or
    bound to its round.

Extending it here needs a monotone per-request sequence in the receipt intent, and a
durable request identity on Claude's failure path.

The case-deadline tolerance does not reach them either, for a different reason: nothing in this
evidence NAMES an evaluator case-deadline kill. aider/codex book budget exhaustion with a
per-case CASE_DEADLINE_INFLIGHT_ALLOWANCE in their own classifier and write no marker into
the receipt tree; Claude's broker rows carry no abort reason and no deadline counter. A
killed request therefore looks exactly like a provider failure here, and guessing is
precisely what the shared contract must not do: a request directory holding failure.json,
or a Claude row with completed False, still refuses. Extending that one here needs the same
self-identifying evidence the two ledger families already carry -- an explicit
`deadline:` request state in the receipt tree, or a deadline counter on Claude's stats.
"""
from __future__ import annotations

import hashlib
import json
import re
import stat

from v2_usage_normalizers import canonical, require, tokens


class ReceiptNormalizer:
    def __init__(self, task):
        require(task in ("aider", "codex", "claude"), "unsupported receipt task")
        self.task = task

    def __call__(self, raw, request_id):
        raise ValueError("receipt index requires validator-owned artifact capability")

    def normalize_with_artifacts(self, index, request_id, access):
        require(isinstance(index, dict) and index.get("schema_version") == "agentswe-evaluator-receipt-index/v1" and
                index.get("task") == self.task and index.get("run_id") == access["run_id"] and
                index.get("owner") == "evaluator", "receipt index ownership/task mismatch")
        if self.task == "claude":
            return self._claude(index, request_id, access)
        return self._directory(index, request_id, access)

    def _directory(self, index, request_id, access):
        root = access["directory"](index["receipt_directory"])
        for entry in root.rglob("*"):
            mode = entry.lstat().st_mode
            require(stat.S_ISREG(mode) or stat.S_ISDIR(mode), "raw receipt tree contains symlink/special entry")
        names = {p.name for p in root.iterdir()}
        require(".process.lock" in names and (root / ".process.lock").is_file(), "receipt process lock missing")
        names.remove(".process.lock")
        require(names and all(re.fullmatch("[0-9a-f]{64}", name) for name in names), "unexpected request directory entry")
        require(request_id in names, "request ID absent from raw receipt directory")
        results = {}
        response_ids = set()
        from responses_stream import strict_json
        for name in sorted(names):
            path = root / name
            # `upstream.raw` is the provider's unmodified response, written by
            # the task's own broker beside the receipt. It is evidence, so it is
            # permitted; every other extra name is still refused, which keeps
            # failure.json and recovered_interruption.json -- the markers this
            # check exists to catch -- rejected exactly as before.
            required = {"intent.json", "upstream_started.json", "completed.json", "response.bin"}
            optional = {"upstream.raw"}
            present = {p.name for p in path.iterdir()} if path.is_dir() else set()
            # Release fix (RC, 2026-10): a failed / recovered / incomplete receipt blocks only when it is the request
            # being normalized. The exporters normalize every request attributed to an accepted or scored round one by
            # one, so an attributed failure still blocks; a sibling the exporter left unattributed (a provider
            # transient during an attempt the run itself classified as infrastructure) no longer makes the whole
            # bundle unexportable, as it did in the paper. Exporters list such receipts, with the
            # sha256 of their files, in the bundle.
            if name != request_id and path.is_dir() and not (required <= present and not (present - required - optional)):
                continue
            require(path.is_dir() and required <= present and not (present - required - optional),
                    "request incomplete, failed, recovered, or contains unexpected artifact: "
                    "%s missing=%s unexpected=%s" % (name, sorted(required - present),
                                                     sorted(present - required - optional)))
            intent = strict_json((path / "intent.json").read_bytes())
            started = strict_json((path / "upstream_started.json").read_bytes())
            completed = strict_json((path / "completed.json").read_bytes())
            payload = (path / "response.bin").read_bytes()
            # The provider's own bytes when the broker kept them. A chat-mode
            # broker rewrites the Responses body into a chat.completion before
            # storing response.bin, which then has no `status` and chat-shaped
            # usage -- so checking completion against it fails for a request the
            # provider really did complete. response.bin keeps its own integrity
            # check against completed.payload_sha256 below either way.
            upstream = path / "upstream.raw"
            provider_payload = upstream.read_bytes() if upstream.is_file() else payload
            require(intent.get("identity") == name and intent.get("protocol") == "agentswe-lower-single-upstream/v1" and
                    isinstance(intent.get("body_sha256"), str) and re.fullmatch("[0-9a-f]{64}", intent["body_sha256"]),
                    "raw intent identity/protocol malformed")
            require(started == {"actual_upstream_requests": 1, "identity": name}, "upstream start not proven")
            require(completed.get("identity") == name and completed.get("status") == 200 and
                    type(completed.get("actual_upstream_requests")) is int and completed["actual_upstream_requests"] == 1 and
                    completed.get("completed_responses") == 1 and
                    completed.get("payload_sha256") == hashlib.sha256(payload).hexdigest(), "raw completion/response integrity mismatch")
            content_type = completed.get("content_type", "").split(";", 1)[0].strip()
            if content_type == "application/json":
                response = strict_json(provider_payload)
            elif content_type == "text/event-stream":
                response = self._sse_response(provider_payload)
            else:
                raise ValueError("unsupported actual response content type")
            # A response the provider finished inside the Candidate's own output
            # budget is delivered, not failed: same shape the brokers admit, and
            # its usage is fully known. Nothing else about the response moves.
            budget_limited = (isinstance(response, dict) and response.get("status") == "incomplete"
                              and isinstance(response.get("incomplete_details"), dict)
                              and response["incomplete_details"].get("reason") == "max_output_tokens")
            require(isinstance(response, dict)
                    and (response.get("status") == "completed" or budget_limited)
                    and response.get("model") == "deepseek-flash" and response.get("error") is None,
                    "provider response not completed/model mismatch")
            response_id = response.get("id")
            require(isinstance(response_id, str) and bool(response_id) and response_id not in response_ids,
                    "provider response ID missing/reused")
            response_ids.add(response_id)
            require(response.get("usage") == completed.get("usage"), "receipt usage differs from original response")
            results[name] = canonical(name, tokens(completed.get("usage")))
        terminal = access["read_json"](index["broker_terminal"])
        require(terminal.get("run_id") == access["run_id"] and terminal.get("owner") == "evaluator" and
                terminal.get("process_reaped") is True and terminal.get("state") == "terminal" and
                terminal.get("receipt_tree_digest") == index["receipt_directory"]["tree_digest"],
                "receipt broker termination not bound")
        return results[request_id]

    @staticmethod
    def _sse_response(payload):
        from responses_stream import ResponseEvents
        decoder = ResponseEvents()
        decoder.feed(payload)
        return decoder.finish()

    def _claude(self, index, request_id, access):
        raw = access["read_json"](index["broker_stats"])
        require(raw.get("schema_version") == "agentswe-broker-stats/v1" and
                raw.get("protocol") == {"model": "deepseek-flash", "reasoning_effort": "high"}, "Claude broker schema/model mismatch")
        lifecycle = raw.get("lifecycle", {})
        rows = raw.get("request_ledger")
        require(isinstance(rows, list) and rows, "Claude requests absent")
        require(lifecycle.get("schema_version") == "agentswe-broker-lifecycle/v1" and
                lifecycle.get("state") == "closed" and lifecycle.get("server_close_completed") is True and
                all(type(lifecycle.get(k)) is int and lifecycle[k] == v for k, v in {
                    "handlers_started": len(rows), "handlers_finished": len(rows), "in_flight": 0}.items()),
                "Claude original stats lack final joined-handler accounting")
        runtime = raw.get("runtime", {})
        require(all(type(runtime.get(k)) is int and runtime[k] == v for k, v in {
            "calls": len(rows), "successful_calls": len(rows), "failures": 0,
            "upstream_transport_attempts": len(rows), "completed_responses": len(rows),
            "unknown_usage_requests": 0}.items()), "Claude aggregate failed/unknown/mismatch")
        require(raw.get("delivery", {}).get("client_delivery_failures") == 0, "Claude response delivery failed")
        require([row.get("logical_request") for row in rows] == list(range(1, len(rows) + 1)), "Claude logical request sequence incomplete")
        ids = [row.get("response_id") for row in rows]
        require(all(isinstance(rid, str) and rid for rid in ids) and len(set(ids)) == len(ids) and request_id in ids,
                "Claude provider response identity absent/reused")
        for row in rows:
            require(row.get("transport_attempts") == 1 and row.get("completed") is True and row.get("status_code") == 200 and
                    row.get("submission_state") == "completed" and row.get("usage_known") is True and row.get("automatic_retry") is False,
                    "Claude request failed/unknown")
            tokens(row.get("usage"))
        require(tokens(runtime) == sum(tokens(row["usage"]) for row in rows), "Claude token aggregate mismatch")
        require(all(runtime[key] == sum(row["usage"][key] for row in rows) for key in
                    ("input_tokens", "output_tokens", "total_tokens")), "Claude token component aggregate mismatch")
        terminal = access["read_json"](index["broker_terminal"])
        require(terminal.get("run_id") == access["run_id"] and terminal.get("owner") == "evaluator" and
                terminal.get("process_reaped") is True and terminal.get("state") == "terminal" and
                terminal.get("stats_sha256") == index["broker_stats"]["sha256"], "Claude final stats/process observation mismatch")
        return canonical(request_id, tokens(rows[ids.index(request_id)]["usage"]))


def make_receipt_lower_normalizer(task):
    return ReceiptNormalizer(task)
