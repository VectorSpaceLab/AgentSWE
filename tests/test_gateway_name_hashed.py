"""The private gateway name appears nowhere in the repository, in any spelling or split form.

    python3 -m unittest tests/test_gateway_name_hashed.py      (standard library only; no docker, no network)

Candidates and judge outputs archived before the release name their gateway provider counts with a private prefix.
runners/creation/provider_counts_compat.py keeps only that prefix's sha256 and recognises a legacy key by hashing its
prefix; the verifier and evaluator scripts that run inside containers carry an identical inline copy. This test
recovers the prefix from the digest at run time (it is short) and never writes it down, then checks that:

1. legacy, neutral, mixed and near-miss reports are normalised exactly as the pre-release name table did;
2. every inline copy is byte-identical to INLINE_HELPER and behaves like the module;
3. the release scanner rejects the name in plain, upper-case and split forms (Python literals joined implicitly or
   with `+`, and quoted fragments inside other text), and no tracked file holds it in any of these forms;
4. no legacy environment name is injected into a Candidate container;
5. the Editing Builder retry policy is chosen by the provider endpoint alone, not by the machine's host name.
"""
from __future__ import annotations

import copy
import functools
import hashlib
import itertools
import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runners" / "creation"))
sys.path.insert(0, str(ROOT / "tools"))
import candidate_broker_protocol as protocol  # noqa: E402
import provider_counts_compat as compat  # noqa: E402
import scan  # noqa: E402

INLINE_COPIES = sorted(
    [f"tasks/creation/{task}/adapter/eval-template/tests/verify_score.py" for task in (
        "authorized-vulnerability-validation", "database-analytics", "desktop-gui-automation",
        "document-to-editable-pptx", "evidence-grounded-document-qa", "formal-theorem-proving",
        "repository-bug-repair", "schema-guided-web-extraction", "scientific-pdf-translation",
        "web-research-report")]
    + ["tasks/creation/database-analytics/adapter/task-template/tests/score_case.py",
       "tasks/creation/database-analytics/benchmark/evaluator/validate_artifacts.py",
       "tasks/creation/repository-bug-repair/adapter/task-template/tests/score_case.py",
       "tasks/creation/repository-bug-repair/benchmark/evaluator/harness/evaluate_case.py"])
SUFFIXES = ("", "_text", "_image", "_image_requests")


@functools.lru_cache(maxsize=None)
def legacy_prefix() -> str:
    """The short lower-case alphanumeric word whose sha256 the compat module keeps, found by search."""
    for size in range(1, 5):
        for chars in itertools.product(string.ascii_lowercase + string.digits, repeat=size):
            word = "".join(chars)
            if hashlib.sha256(word.encode()).hexdigest() == compat.LEGACY_GATEWAY_SHA256:
                return word
    raise AssertionError("the legacy digest has no short alphanumeric preimage")


def pre_release(value):
    """The pre-release normaliser: an explicit table of the four legacy keys (rebuilt from the recovered prefix)."""
    table = {legacy_prefix() + s: "gateway" + s for s in SUFFIXES}

    def walk(v):
        if isinstance(v, dict):
            legacy = [k for k in v if k in table]
            if legacy and not any(table[k] in v for k in legacy):
                v = {table.get(k, k): x for k, x in v.items()}
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v
    return walk(value)


def inline_namespace() -> dict:
    namespace: dict = {}
    exec(compile(compat.INLINE_HELPER, "INLINE_HELPER", "exec"), namespace)
    return namespace


def reports():
    p = legacy_prefix()
    legacy = {"status": "success", "elapsed_seconds": 12.5, p: 4, p + "_image_requests": 1,
              "provider_counts": {p + "_text": 3, p + "_image": 1, "serper": 0, "web_retrieval": 0},
              "errors": [f"{p}_text quota note kept verbatim"], "attempts": [{p + "_text": 1}, {"gateway_text": 2}]}
    neutral = {"status": "success", "gateway": 4, "gateway_image_requests": 1,
               "provider_counts": {"gateway_text": 3, "gateway_image": 1, "serper": 0, "web_retrieval": 0}}
    mixed = {"provider_counts": {p + "_text": 1, "gateway_text": 1, p + "_image": 0}}
    partial = {"provider_counts": {p + "_text": 1, "gateway_image": 0}}
    near = {p.upper(): 1, p.upper() + "_TEXT": 1, p + "_TEXT": 1, p + "_other": 1, "x" + p: 1, p + "_text_": 1,
            p + "_image_requests_total": 1, "_text": 1, "": 1, "gateway_text": 0, 7: "int key", None: "none key"}
    return {"legacy": legacy, "neutral": neutral, "mixed": mixed, "partial": partial, "near": near}


class LegacyKeys(unittest.TestCase):
    def test_digest_is_the_only_spelling(self):
        self.assertEqual(len(compat.LEGACY_GATEWAY_SHA256), 64)
        self.assertIn(compat.LEGACY_GATEWAY_SHA256, compat.INLINE_HELPER)
        self.assertFalse(hasattr(compat, "LEGACY_GATEWAY"))
        self.assertFalse(hasattr(compat, "LEGACY_ENV_ALIASES"))

    def test_legacy_report_normalised_as_before(self):
        p, report = legacy_prefix(), reports()["legacy"]
        before = copy.deepcopy(report)
        out = compat.neutral_provider_keys(report)
        self.assertEqual(report, before)  # the input is not modified
        self.assertEqual(out, pre_release(report))
        self.assertEqual(out, {
            "status": "success", "elapsed_seconds": 12.5, "gateway": 4, "gateway_image_requests": 1,
            "provider_counts": {"gateway_text": 3, "gateway_image": 1, "serper": 0, "web_retrieval": 0},
            "errors": [f"{p}_text quota note kept verbatim"], "attempts": [{"gateway_text": 1}, {"gateway_text": 2}]})
        self.assertEqual(list(out), ["status", "elapsed_seconds", "gateway", "gateway_image_requests",
                                     "provider_counts", "errors", "attempts"])  # key order kept
        for suffix in SUFFIXES:
            self.assertEqual(compat.neutral_name(p + suffix), "gateway" + suffix)

    def test_neutral_report_unchanged(self):
        report = reports()["neutral"]
        self.assertEqual(compat.neutral_provider_keys(report), report)
        self.assertEqual(json.dumps(compat.neutral_provider_keys(report)), json.dumps(report))

    def test_mixed_report_unchanged_as_before(self):
        p, mixed = legacy_prefix(), reports()["mixed"]
        self.assertEqual(compat.neutral_provider_keys(mixed), mixed)
        self.assertEqual(compat.neutral_provider_keys({p: 1, "gateway": 2}), {p: 1, "gateway": 2})
        # an object whose legacy keys have no neutral partner is renamed, as before
        partial = reports()["partial"]
        self.assertEqual(compat.neutral_provider_keys(partial), pre_release(partial))
        self.assertEqual(compat.neutral_provider_keys(partial), {"provider_counts": {"gateway_text": 1,
                                                                                     "gateway_image": 0}})

    def test_near_misses_and_odd_keys_untouched(self):
        near = reports()["near"]
        self.assertEqual(compat.neutral_provider_keys(near), near)
        self.assertEqual(compat.neutral_provider_keys(near), pre_release(near))
        self.assertIsNone(compat.neutral_name("\ud800" + legacy_prefix()))  # a lone surrogate does not raise
        for scalar in (None, 3, "text", 1.5, True):
            self.assertEqual(compat.neutral_provider_keys(scalar), scalar)

    def test_random_reports_match_pre_release(self):
        p = legacy_prefix()
        pool = [p + s for s in SUFFIXES] + ["gateway" + s for s in SUFFIXES] + [
            p.upper(), p + "_TEXT", "serper", "web_retrieval", "status", "x" + p, p + "_texts"]
        rng = random.Random(20261012)
        inline = inline_namespace()["_agentswe_neutral_provider_keys"]

        def make(depth):
            if depth == 0 or rng.random() < 0.3:
                return rng.choice([0, 1, "v", None, p])
            if rng.random() < 0.3:
                return [make(depth - 1) for _ in range(rng.randint(0, 3))]
            return {rng.choice(pool): make(depth - 1) for _ in range(rng.randint(0, 5))}
        for _ in range(3000):
            value = make(4)
            expected = pre_release(value)
            self.assertEqual(compat.neutral_provider_keys(value), expected)
            self.assertEqual(inline(value), expected)


class InlineCopies(unittest.TestCase):
    def test_every_copy_is_byte_identical(self):
        block = compat.INLINE_HELPER.strip("\n")
        marker = "# --- AgentSWE release compat: legacy gateway count keys"
        found = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tasks").rglob("*.py")
                       if marker in p.read_text(encoding="utf-8", errors="replace"))
        self.assertEqual(found, INLINE_COPIES)
        for rel in INLINE_COPIES:
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertEqual(text.count(block), 1, rel)
            self.assertEqual(text.count(marker), 1, rel)
            self.assertIn("_agentswe_neutral_provider_keys(", text.replace(block, ""), rel)  # and it is called

    def test_inline_helper_behaves_like_the_module(self):
        inline = inline_namespace()
        for name, report in reports().items():
            with self.subTest(name):
                self.assertEqual(inline["_agentswe_neutral_provider_keys"](report),
                                 compat.neutral_provider_keys(report))
        for suffix in SUFFIXES:
            self.assertEqual(inline["_agentswe_neutral_name"](legacy_prefix() + suffix), "gateway" + suffix)
        self.assertEqual(inline["_AGENTSWE_LEGACY_GATEWAY_SHA256"], compat.LEGACY_GATEWAY_SHA256)


class Scanner(unittest.TestCase):
    def setUp(self):
        self.terms = scan.load_terms(ROOT)
        self.rule = "term:" + compat.LEGACY_GATEWAY_SHA256[:10]

    def rules(self, hits):
        return {name for _, name in hits}

    def test_terms_hold_the_name(self):
        self.assertIn(compat.LEGACY_GATEWAY_SHA256, self.terms)

    def test_plain_and_split_forms_are_rejected(self):
        p = legacy_prefix()
        for text in (f"routed through {p} today", f"{p.upper()}_API_KEY=x", f"key {p}_image_requests"):
            self.assertIn(self.rule, self.rules(scan.term_hits(text, self.terms)), text)
        pieces = [(p[:i], p[i:]) for i in range(1, len(p))]
        sources = [f'NAME = "{a}" "{b}"\n' for a, b in pieces]
        sources += [f'ENV = "{a.upper()}" + "{b.upper()}_API_KEY"\n' for a, b in pieces]
        sources += [f'KEYS = (\n    "{a}"  # first part\n    "{b}_text"\n)\n' for a, b in pieces]
        for src in sources:
            self.assertNotIn(self.rule, self.rules(scan.term_hits(src, self.terms)), src)  # plain text misses it
            self.assertIn(self.rule, self.rules(scan.split_term_hits(src, self.terms, True)), src)
        held = f"HELPER = '''\nPREFIX = \"{p[:1]}\" \"{p[1:]}\"\n'''\n"  # code held inside a string
        self.assertIn(self.rule, self.rules(scan.split_term_hits(held, self.terms, True)))
        shell = f'export X="{p[:2]}""{p[2:]}"\n'
        self.assertIn(self.rule, self.rules(scan.split_term_hits(shell, self.terms, False)))
        # pieces that do not join stay apart
        apart = f'A = ["{p[:1]}", "{p[1:]}"]\nB = "{p[:1]}" * 2 + "x"\n'
        self.assertEqual(scan.split_term_hits(apart, self.terms, True), [])

    def test_scan_command_fails_on_a_split_name(self):
        p = legacy_prefix()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            (root / "tools" / "scan_terms.sha256").write_bytes((ROOT / "tools" / "scan_terms.sha256").read_bytes())
            (root / "clean.py").write_text('GATEWAY = "gateway"\n')
            (root / "split.py").write_text(f'PREFIX = "{p[:1]}" "{p[1:]}"\n')
            done = subprocess.run([sys.executable, "-B", str(ROOT / "tools" / "scan.py"), str(root)],
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 1, done.stdout)
            self.assertIn("split.py:1:" + self.rule, done.stdout)
            self.assertNotIn("clean.py", done.stdout)
            self.assertNotIn(p, done.stdout.lower().replace(self.rule, "").replace(str(root).lower(), ""))

    def test_tree_has_no_form_of_the_name(self):
        p = legacy_prefix()
        listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True)
        rels = [r for r in listed.stdout.decode().split("\0") if r] if listed.returncode == 0 else [
            q.relative_to(ROOT).as_posix() for q in ROOT.rglob("*") if q.is_file() and ".git" not in q.parts]
        bounded = re.compile(rb"(?<![A-Za-z0-9])" + re.escape(p.encode()) + rb"(?![A-Za-z0-9])", re.I)
        offenders = []
        for rel in rels:
            path = ROOT / rel
            if not path.is_file():
                continue
            data = path.read_bytes()
            text = scan.text_of(data)
            if text is None:
                continue  # binary assets: chance byte matches are not names
            if bounded.search(data):
                offenders.append(rel + ": plain")
            if self.rule in self.rules(scan.split_term_hits(text, {compat.LEGACY_GATEWAY_SHA256},
                                                             rel.endswith(".py"))):
                offenders.append(rel + ": split")
        self.assertEqual(offenders, [])


class CandidateEnvironment(unittest.TestCase):
    def test_no_legacy_environment_names(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENTSWE_SEARCH_FRONT", None)
            cred = Path(tmp) / "candidate.env"
            cred.write_text("GATEWAY_API_KEY=broker-only-placeholder\n")
            config = protocol.configure_candidate_compose({"services": {"main": {"volumes": [
                {"type": "bind", "source": str(cred), "target": protocol.CONTAINER_CREDENTIAL_SUFFIX}],
                "environment": {}}}})
        env = config["services"]["main"]["environment"]
        self.assertEqual(env["GATEWAY_API_KEY"], "broker-only-placeholder")
        self.assertEqual(env["GATEWAY_RESPONSES_ENDPOINT"], protocol.candidate_endpoint())
        for name in env:
            for i in range(1, len(name) + 1):
                self.assertNotEqual(hashlib.sha256(name[:i].lower().encode()).hexdigest(),
                                    compat.LEGACY_GATEWAY_SHA256, name)


class BuilderRetryPolicy(unittest.TestCase):
    """runners/editing/control/builder_segments.py pins the gateway transport retry policy by endpoint only."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "runners" / "editing" / "control"))
        import builder_segments
        self.seg = builder_segments
        self.work = Path(tempfile.mkdtemp(prefix="agentswe-gw-test-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.calls = []
        fake = types.ModuleType("readiness_binding")
        fake.configure_native_transport = lambda path: self.calls.append(Path(path)) or {"request_max_retries": 10}
        for patcher in (mock.patch.dict(sys.modules, {"readiness_binding": fake}),
                        mock.patch.object(builder_segments, "ensure_importable", lambda: None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def job(self, provider_text: str) -> Path:
        provider = self.work / "config.toml"
        provider.write_text(provider_text)
        config = self.work / "job.json"
        config.write_text(json.dumps({"agents": [{"env": {self.seg.CONFIG_TOML_ENV: str(provider)}}]}))
        return config

    def test_gateway_provider_pinned_whatever_the_host(self):
        config = self.job(f'base_url = "https://{self.seg.GATEWAY_ENDPOINT_MARKER}/v1"\n')
        for host in ("builder-167", "node167", "builder-1"):
            receipt = self.seg.pin_native_retry_policy(self.work / host, config, hostname=host)
            self.assertTrue(receipt["applied"], (host, receipt))
            self.assertEqual(receipt["hostname"], host)
        self.assertEqual(len(self.calls), 3)

    def test_other_provider_left_alone_whatever_the_host(self):
        text = 'base_url = "https://api.deepseek.com/v1"\n'
        config = self.job(text)
        for host in ("builder-167", "builder-1"):
            receipt = self.seg.pin_native_retry_policy(self.work / host, config, hostname=host)
            self.assertFalse(receipt["applied"])
            self.assertEqual(receipt["skipped"], "provider config is not a gateway endpoint")
        self.assertEqual(self.calls, [])
        self.assertEqual((self.work / "config.toml").read_text(), text)
        self.assertFalse(hasattr(self.seg, "host_is_gateway"))


if __name__ == "__main__":
    unittest.main()
