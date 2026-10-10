#!/usr/bin/env python3
"""Evaluator-owned dynamic case facts and redacted Candidate projection."""
from __future__ import annotations
import hashlib, json, secrets
from dataclasses import dataclass, asdict
from pathlib import Path

@dataclass(frozen=True)
class PrivateFacts:
    case_id: str
    route_provider: str
    route_account: str
    route_peer: str
    route_thread: str | None
    task_nonce: str
    owner_epoch: int
    grant_epoch: int
    callback_token: str
    attachment_sha256: str
    oracle_decision: str
    bundle_version: str = "openclaw-native-case-v2"
    attachment_size: int = 0

CASES = ("dev_001", "dev_002", "test_001", "test_002", "test_003", "test_004", "test_005", "test_006")

def attachment_bytes(nonce: str, size: int) -> bytes:
    # Runtime-private seed determines a real task asset, not a hidden answer
    # or a hard-coded provider response. Largest legal chunks still require
    # three blocks in hidden005.
    data = bytearray()
    for index in range((size + 31) // 32):
        data.extend(hashlib.sha256((nonce + ":media:" + str(index)).encode()).digest())
    return bytes(data[:size])

def make_private_facts(case_id: str, attachment: bytes | None = None) -> PrivateFacts:
    # WebChat is OpenClaw's built-in Gateway channel.  It exercises the real
    # channel-aware agent path without inventing an unregistered adapter.
    if case_id not in CASES:
        raise ValueError("case is outside the frozen 2 dev + 6 hidden inventory")
    nonce = secrets.token_hex(16)
    if attachment is None:
        size = 32771 if case_id == "test_005" else 3073 if case_id == "dev_002" else 0
        attachment = attachment_bytes(nonce, size)
    return PrivateFacts(case_id, "webchat", "default", f"peer-{secrets.token_hex(6)}", f"thread-{secrets.token_hex(5)}" if case_id in {"dev_002","test_004","test_005"} else None, nonce, 1, 1, secrets.token_urlsafe(24), hashlib.sha256(attachment).hexdigest(), "evaluator-only", attachment_size=len(attachment))

def redacted_projection(facts: PrivateFacts) -> dict[str, object]:
    return {"case_id": facts.case_id, "channel": {"provider": facts.route_provider, "account_id": "default", "peer_id": "case-peer", **({"thread_id": "case-thread"} if facts.route_thread else {})}, "available_actions": ["agent", "agent.wait", "sessions.send", "chat.send", "sessions.compact"], "visible_receipt_fields": ["status", "state", "revision", "replayed", "provider_message_present"], "oracle_disclosed": False}

def write_private_case(root: Path, case_id: str) -> tuple[Path, dict[str, object]]:
    root.mkdir(parents=True, exist_ok=True)
    private, view = root / "private_facts.json", root / "candidate_view.json"
    if private.exists() or view.exists():
        raise ValueError("refusing to replace an existing runtime case bundle")
    facts = make_private_facts(case_id)
    private.write_text(json.dumps(asdict(facts), indent=2) + "\n")
    view.write_text(json.dumps(redacted_projection(facts), indent=2) + "\n")
    return private, redacted_projection(facts)
