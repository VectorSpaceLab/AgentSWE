"""Persisted one-attempt lower request ledger; adapted from the validated OpenHands mechanism."""
from __future__ import annotations
import hashlib,json,os,threading
from pathlib import Path
from typing import Any
MODEL='deepseek-flash';LOWER_EFFORT='high';SCHEMA='deepcode-request-ledger/v2'
MEASUREMENT='http-request-start-after-connect-and-tls/v1'
class Stats:
    def __init__(self, path: Path | None = None, *, effort: str = LOWER_EFFORT, role: str = "lower") -> None:
        self.lock = threading.Lock()
        self.path = path
        self.effort = effort
        self.role = role
        self.value: dict[str, Any] = {
            "schema_version": SCHEMA,
            "role": role,
            "transport_measurement": MEASUREMENT,
            "protocol": {"model": MODEL, "reasoning_effort": effort, "transport": "responses"},
            "runtime": {"calls": 0, "failures": 0, "successful_calls": 0,
                         "input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "requests": [],
        }
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                previous = json.loads(self.path.read_text())
                if previous.get("protocol") != self.value["protocol"] or previous.get("role") != role:
                    raise RuntimeError("existing broker ledger protocol mismatch")
                self.value = previous
            else:
                self._save()

    def _save(self):
        if self.path:
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            with temporary.open("w") as handle:
                json.dump(self.value,handle,indent=2);handle.write("\n");handle.flush();os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            fd=os.open(self.path.parent,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)

    def reserve(self, request_id, context_id=None):
        with self.lock:
            intents = self.value.setdefault("logical_requests", {})
            existing = intents.get(request_id)
            if existing is not None: return json.loads(json.dumps(existing))
            if context_id and any(item.get('context_id')==context_id and item.get('state')!='completed' for item in intents.values()):
                return {'state':'blocked_by_prior_unknown_context','retry_allowed':False}
            if self.value.get('transport_measurement') != MEASUREMENT:
                raise ValueError('legacy ledger is read-only for new requests; preserve prior identities')
            intents[request_id] = {"state": "reserved_not_submitted", "request_sha256": request_id,
                'context_id':context_id, 'transport_attempts':0}
            self._save()
            return None

    def transport_started(self, request_id):
        """Persist a possible model POST immediately before the first HTTP send.

        CONNECT and TLS failures happen before this callback. Never grant a
        second transport attempt, including after a failed or unknown result.
        """
        with self.lock:
            intent=self.value['logical_requests'][request_id]
            if intent.get('state')!='reserved_not_submitted' or intent.get('transport_attempts')!=0:
                raise ValueError('logical request transport may start only once')
            intent.update(state='submitted_or_unknown',transport_attempts=1)
            self.value['runtime']['calls']+=1
            self._save()

    def record(self, *, ok: bool, usage: dict[str, Any] | None, error: str | None,
               request_id: str | None = None, response: dict | None = None,
               output_budget_truncated: bool = False) -> None:
        """``output_budget_truncated`` books an answer that stopped on its
        max_output_tokens ceiling as something OTHER than a delivered answer.

        Deliberately additive.  The transport really completed and the tokens
        were really billed, so ok, upstream_completion="completed", the
        successful_calls counter and the intent state are all left exactly as
        they were: the shared usage normalizers require
        ``successful_calls == len(rows)`` and ``upstream_completion ==
        "completed"`` on every non-recovered row
        (harbor/0905-edit-case-repair/v2_usage_normalizers.py:141-156,
        v2_extra_usage_normalizers.py:92-121), and booking it as a failure would
        additionally wedge the whole context, because reserve() blocks every
        later request of a context that holds a non-completed intent.  What this
        adds instead is the truth beside them: ``delivered_answer: False`` and
        ``output_budget_truncated: True`` on the row, a separate
        ``output_budget_truncations`` runtime counter, and a flag on the logical
        request so the broker refuses to replay it to the Candidate's retry.
        """
        with self.lock:
            if self.value.get('transport_measurement') != MEASUREMENT:
                return  # Original ledgers remain immutable; only cache reads are allowed.
            runtime = self.value["runtime"]
            intent=self.value.get('logical_requests',{}).get(request_id,{})
            attempts=intent.get('transport_attempts',0)
            if attempts not in (0,1):raise ValueError('invalid transport attempt count')
            if attempts:
                runtime["successful_calls" if ok else "failures"] += 1
                if ok and output_budget_truncated:
                    runtime['output_budget_truncations'] = runtime.get('output_budget_truncations', 0) + 1
            elif not ok:
                runtime['pre_transport_failures']=runtime.get('pre_transport_failures',0)+1
            known = isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k]>=0 for k in ('input_tokens','output_tokens','total_tokens'))
            runtime.setdefault("unknown_usage_calls", 0)
            if attempts and not known: runtime["unknown_usage_calls"] += 1
            tokens = {name: usage[name] if attempts and known else (None if attempts else 0) for name in ("input_tokens", "output_tokens", "total_tokens")}
            for name, value in tokens.items():
                if value is not None: runtime[name] += value
            row = {"ok": ok, "model": MODEL, "reasoning_effort": self.effort, **tokens,
                   "usage_state": ("known" if known else "unknown") if attempts else "not_submitted", "error": error, "request_sha256": request_id,
                   "transport_attempts": attempts, "upstream_completion": ("completed" if ok else "unknown_or_failed") if attempts else "not_submitted",
                   "delivered_answer": bool(ok and attempts and not output_budget_truncated)}
            if ok and attempts and output_budget_truncated:
                row["output_budget_truncated"] = True
            self.value["requests"].append(row)
            if request_id:
                intent = self.value.setdefault("logical_requests", {}).setdefault(request_id, {})
                if ok and not attempts:raise ValueError('completed model response has no observed HTTP start')
                intent.update(state=("completed" if ok else "unknown_or_failed") if attempts else "pre_transport_failed", result=row)
                if ok and attempts and output_budget_truncated:
                    # The upstream call did finish and must never be re-attempted
                    # under THIS identity; the flag is what stops the replay and
                    # sends the Candidate's retry to a fresh identity instead
                    # (broker_server.do_POST, TRUNCATED_RETRY_MAX).
                    intent['output_budget_truncated'] = True
                if response is not None:
                    # Response bodies contain no request credential and stay evaluator-private.
                    if self.path:
                        raw_dir = self.path.parent / (self.path.stem + "-responses")
                        raw_dir.mkdir(exist_ok=True)
                        raw_path = raw_dir / (request_id + ".json")
                        with raw_path.open("x") as handle:
                            json.dump(response,handle);handle.flush();os.fsync(handle.fileno())
                        intent.update(response_path=str(raw_path), response_sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest())
                    else: intent["response"] = response
            self._save()

    def completed_response(self, intent):
        if intent.get("state") != "completed": return None
        if "response" in intent: return intent["response"]
        path = Path(intent["response_path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != intent["response_sha256"]:
            raise RuntimeError("completed broker response hash mismatch")
        return json.loads(path.read_text())

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            result = json.loads(json.dumps(self.value))
        result["requests"] = result["requests"][-5000:]  # was 100: hardened tasks / 5 formal rounds exceed it (0919)
        if result.get('transport_measurement')==MEASUREMENT:
            runtime=result['runtime'];intents=result.get('logical_requests',{})
            pending=sum(v.get('transport_attempts')==1 and v.get('state')=='submitted_or_unknown' for v in intents.values())
            runtime['pending_calls']=pending
            runtime['known_usage_subtotal']={k:runtime[k] for k in ('input_tokens','output_tokens','total_tokens')}
            runtime.setdefault('output_budget_truncations',0)
            runtime['unknown_usage_calls']=runtime.get('unknown_usage_calls',0)+pending
            if runtime['unknown_usage_calls']:
                for k in ('input_tokens','output_tokens','total_tokens'):runtime[k]=None
        return result
