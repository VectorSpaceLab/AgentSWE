"""Neutral gateway provider-count names with backward-compatible reading.

The release renames the evaluator gateway's provider-count keys (layout contract
0.8: a private gateway prefix -> ``gateway``):

    <legacy>                -> gateway                 (all gateway model requests)
    <legacy>_text           -> gateway_text            (text-only requests)
    <legacy>_image          -> gateway_image           (image-bearing requests)
    <legacy>_image_requests -> gateway_image_requests

Candidates built before the release (every paper and Lite candidate) still write
the legacy keys in ``run_report.json``, and archived judge outputs carry them too.
Evaluators call :func:`neutral_provider_keys` on every such report before any
check, so an old report is judged exactly as it was, and a new one with neutral
keys is judged identically.  A report that mixes both schemes in one object is
left unchanged and therefore fails the evaluator's exact-key checks, which is the
pre-release behaviour for any unexpected key.

The legacy prefix is not written anywhere in the repository: only its SHA-256 is
kept, and a key is legacy when it is one of the four suffixes above appended to a
prefix with that digest.
"""
from __future__ import annotations

import hashlib
from typing import Any

NEUTRAL_GATEWAY = "gateway"
LEGACY_GATEWAY_SHA256 = "1fce5ffd30ad7f89f5dc915b9796f5dceb1a89a39deb4a40f0c67b9f516b3222"
SUFFIXES = ("", "_text", "_image", "_image_requests")


def neutral_name(key: Any) -> str | None:
    """The neutral name of a legacy gateway count key, or None for any other key."""
    if isinstance(key, str):
        for suffix in SUFFIXES:
            prefix = key[:len(key) - len(suffix)]
            if key.endswith(suffix) and hashlib.sha256(
                    prefix.encode("utf-8", "surrogatepass")).hexdigest() == LEGACY_GATEWAY_SHA256:
                return NEUTRAL_GATEWAY + suffix
    return None


def neutral_provider_keys(value: Any) -> Any:
    """Return ``value`` with legacy gateway count keys renamed, recursively.

    Only dict keys are renamed; values (including free-text error strings) are
    never touched.  An object that already contains the neutral name of any of
    its legacy keys is returned unchanged (mixed schemes stay invalid).
    """
    if isinstance(value, dict):
        renamed = {key: neutral_name(key) for key in value}
        legacy = [key for key in value if renamed[key]]
        if legacy and not any(renamed[key] in value for key in legacy):
            value = {renamed[key] or key: item for key, item in value.items()}
        return {key: neutral_provider_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [neutral_provider_keys(item) for item in value]
    return value


# Inline form for the Harbor verifier and evaluator scripts that run inside eval and
# candidate-task containers and cannot import repository modules.  Each of them
# carries this block byte for byte (tests/test_gateway_legacy_keys.py checks the
# copies and that the block behaves exactly like neutral_provider_keys).
INLINE_HELPER = '''
# --- AgentSWE release compat: legacy gateway count keys (see provider_counts_compat.py) ---
import hashlib as _agentswe_hashlib

_AGENTSWE_LEGACY_GATEWAY_SHA256 = "1fce5ffd30ad7f89f5dc915b9796f5dceb1a89a39deb4a40f0c67b9f516b3222"


def _agentswe_neutral_name(key):
    if isinstance(key, str):
        for suffix in ("", "_text", "_image", "_image_requests"):
            prefix = key[:len(key) - len(suffix)]
            if key.endswith(suffix) and _agentswe_hashlib.sha256(
                    prefix.encode("utf-8", "surrogatepass")).hexdigest() == _AGENTSWE_LEGACY_GATEWAY_SHA256:
                return "gateway" + suffix
    return None


def _agentswe_neutral_provider_keys(value):
    if isinstance(value, dict):
        renamed = {k: _agentswe_neutral_name(k) for k in value}
        legacy = [k for k in value if renamed[k]]
        if legacy and not any(renamed[k] in value for k in legacy):
            value = {renamed[k] or k: v for k, v in value.items()}
        return {k: _agentswe_neutral_provider_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_agentswe_neutral_provider_keys(v) for v in value]
    return value
# --- end AgentSWE release compat ---
'''
