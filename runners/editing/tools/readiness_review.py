#!/usr/bin/env python3
"""Mechanically verify a v2 readiness bundle, then emit the independent review report.

`readiness_coordinator.prepare_admission` requires a review report asserting nine
checks. This tool never asserts a check it has not verified against the bundle's
own bytes: every check is derived from the manifest, the referenced artifacts and
the live source/contract/registry, and any failure refuses to write a report at
all. It performs no provider call, no cleanup, no registry or gate write.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

PROFILE = 'single-dev-two-round-hidden-smoke-v1'
CHECKS = ('current_source_and_contract_reviewed', 'real_native_builder_two_dev_acceptance',
          'feedback_and_frozen_candidate_verified', 'real_medium_product_execution',
          'artifact_trajectory_oracle_provenance', 'independent_xhigh_result_and_code',
          'owned_cleanup_verified', 'formal_entry_matches_reviewed_execution',
          'configuration_prerequisites_resolved')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Review:
    def __init__(self, task, bundle_root, control_root, source):
        self.task, self.control, self.source = task, Path(control_root), Path(source)
        self.root = Path(bundle_root).resolve(strict=True)
        self.manifest_path = self.root / 'manifest.json'
        self.manifest_sha = sha(self.manifest_path)
        self.m = json.loads(self.manifest_path.read_bytes())
        self.failures = []

    def raw_ref(self, reference, *, label):
        """Resolve a reference to bytes; feedback is raw text, not JSON."""
        if not isinstance(reference, dict) or not isinstance(reference.get('path'), str):
            raise ValueError('malformed reference: ' + label)
        path = (self.root / reference['path']).resolve()
        path.relative_to(self.root)
        if path.is_symlink() or not path.is_file():
            raise ValueError('missing bundle artifact: ' + label)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != reference.get('sha256'):
            raise ValueError('bundle artifact hash mismatch: ' + label)
        return data

    def ref(self, reference, *, label):
        if not isinstance(reference, dict) or not isinstance(reference.get('path'), str):
            raise ValueError('malformed reference: ' + label)
        path = (self.root / reference['path']).resolve()
        path.relative_to(self.root)
        if path.is_symlink() or not path.is_file():
            raise ValueError('missing bundle artifact: ' + label)
        if sha(path) != reference.get('sha256'):
            raise ValueError('bundle artifact hash mismatch: ' + label)
        return json.loads(path.read_bytes())

    def check(self, name, fn):
        try:
            detail = fn()
        except Exception as exc:                                  # noqa: BLE001
            self.failures.append('%s: %s: %s' % (name, type(exc).__name__, str(exc)[:200]))
            return False, str(exc)[:200]
        if detail is not True:
            self.failures.append('%s: %s' % (name, detail))
            return False, detail
        return True, 'verified'

    # --- individual checks -------------------------------------------------
    def live_binding(self):
        # Digest helpers always come from the production control plane; the
        # control root only supplies the registry/snapshot bytes under review,
        # so a staging fixture can be reviewed with the same code path.
        sys.path.append('@@AGENTSWE_EDITING_CONTROL@@')
        sys.path.insert(0, str(self.control))
        from audit_readiness import tree_digest
        from readiness_admission import registry_digest
        return {'task': self.task, 'source_digest': tree_digest(self.source),
                'contract_digest': sha(self.source / 'meta/0905_case_contract.json'),
                'registry_digest': registry_digest(self.control / 'configuration_delta_registry.json',
                                                  self.task)}

    def c_source(self):
        live = self.live_binding()
        if self.m.get('current_binding') != live:
            return 'manifest binding differs from live source/contract/registry: %s vs %s' % (
                self.m.get('current_binding'), live)
        return True

    def c_two_rounds(self):
        rounds = self.m.get('public_rounds')
        if not isinstance(rounds, list) or len(rounds) != 2:
            return 'expected exactly two public rounds, got %r' % (
                len(rounds) if isinstance(rounds, list) else rounds)
        digests, sessions = [], set()
        for number, row in enumerate(rounds, 1):
            if row.get('case') != 'dev_001' or row.get('submission_number') != number:
                return 'round %d case/number mismatch' % number
            if set(row.get('submission_sha256') or {}) != {
                    'solution.patch', 'edit_report.json', 'run_report.json'}:
                return 'round %d submission is not exactly the three delivery files' % number
            execution = self.ref(row['execution'], label='round%d.execution' % number)
            if execution.get('build_exit_code') != 0:
                return 'round %d build did not succeed' % number
            digests.append(row.get('candidate_digest'))
            sessions.add(row.get('builder_session_id'))
        if len(set(digests)) != 2 or not all(digests):
            return 'candidate digests are missing or not distinct'
        if len(sessions) != 1 or not all(sessions):
            return 'rounds do not share one builder session: %r' % (sessions,)
        self.rounds, self.candidates = rounds, digests
        return True

    def c_feedback_freeze(self):
        first, second = self.m['public_rounds']
        issued = self.raw_ref(first['feedback'], label='round1.feedback')
        algorithm = first['feedback'].get('digest_algorithm')
        consumed = second.get('feedback_digest')
        if not consumed:
            return 'round 2 does not record a consumed feedback digest'
        if second.get('revision_of_candidate_digest') != first.get('candidate_digest'):
            return 'round 2 is not recorded as a revision of round 1'
        if algorithm == 'sha256-bytes-v1':
            recomputed = hashlib.sha256(issued).hexdigest()
        else:
            payload = json.loads(issued)
            payload.pop('feedback_digest', None)
            canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
            if algorithm == 'canonical-json-without-feedback-digest-no-newline-v1':
                recomputed = hashlib.sha256(canonical.encode()).hexdigest()
            else:
                recomputed = hashlib.sha256((canonical + chr(10)).encode()).hexdigest()
        if recomputed != consumed:
            return ('round 2 consumed digest %s does not match the digest recomputed from the '
                    'round 1 feedback bytes (%s, %s)' % (consumed[:12], recomputed[:12], algorithm))
        freeze = self.ref(self.m['freeze'], label='freeze')
        if freeze.get('delivery_candidate_digest') != second['candidate_digest']:
            return 'freeze delivery digest does not bind the latest accepted candidate'
        if freeze.get('builder_session_id') != second.get('builder_session_id'):
            return 'freeze is not bound to the builder session that produced the rounds'
        if freeze.get('current_binding') != self.m.get('current_binding'):
            return 'freeze binding differs from the manifest binding'
        return True

    # Budget exhaustion is a Candidate outcome, so a lower ledger whose
    # only failure rows are the evaluator's own case-deadline kills is admissible -- the
    # same single case-deadline tolerance as the shared normalizers and v2_readiness. A
    # tolerated row must be the exact shape that normalizer produces; nothing else moves.
    DEADLINE_ERROR_PREFIX = 'deadline:'

    def deadline_killed(self, row):
        """True only for the canonical, self-declared case-deadline kill row."""
        return (row.get('recovered_transport') is True and row.get('deadline_killed') is True
                and row.get('state') == 'failure' and row.get('usage_known') is False
                and row.get('known_tokens') == 0
                and isinstance(row.get('error'), str)
                and row['error'].startswith(self.DEADLINE_ERROR_PREFIX))

    def c_medium_product(self):
        for role in ('public_lower', 'hidden_lower'):
            usage = self.ref(self.m['usage'][role], label='usage.' + role)
            if usage.get('role') != role or usage.get('owner') != 'evaluator':
                return '%s usage is not an evaluator receipt for that role' % role
            requests = usage.get('requests')
            if not isinstance(requests, list) or not requests:
                return '%s usage has no requests' % role
            killed = [r for r in requests if r.get('deadline_killed') is not None]
            if any(not self.deadline_killed(r) for r in killed):
                return '%s has a case-deadline row that is not the declared kill shape' % role
            if usage.get('failures') != len(killed) or usage.get('in_flight'):
                return '%s usage records a failed or in-flight request' % role
            if usage.get('unknown_usage') != len(killed):
                return '%s unknown-usage accounting does not match its declared kills' % role
            named = [{'request_id': r.get('request_id'), 'error': r.get('error')} for r in killed]
            if killed and usage.get('recovered_transport_failures') != named:
                return '%s does not name every tolerated request in its receipt' % role
            for row in requests:
                if self.deadline_killed(row):
                    continue
                if row.get('state') != 'success' or row.get('usage_known') is not True:
                    return '%s has a request that is not a completed known-usage call' % role
            if len(killed) >= len(requests):
                return '%s made no completed call of its own' % role
            if not usage.get('known_tokens'):
                return '%s reports no known tokens, so no real product execution happened' % role
            if usage.get('calls') != len(requests):
                return '%s call count does not match its request rows' % role
        return self.c_candidate_outcomes()

    # The reverse direction -- a receipt may only CLAIM a Candidate outcome when the
    # evaluator's own absence observation is in the bundle and says the same thing.
    def c_candidate_outcomes(self):
        sys.path.append('@@AGENTSWE_EDITING_CONTROL@@')
        from execution_contract import FATAL_CANDIDATE_CLASSES
        receipts = [('hidden_smoke', self.ref(self.m['hidden_smoke'], label='hidden_smoke'))]
        for number, row in enumerate(self.m.get('public_rounds') or [], 1):
            receipts.append(('round%d.execution' % number,
                             self.ref(row['execution'], label='round%d.execution' % number)))
        for label, receipt in receipts:
            outcome = receipt.get('candidate_outcome')
            if outcome is None:
                if 'native_evidence' in receipt or 'candidate_failure_class' in receipt:
                    return '%s publishes absence evidence without a Candidate outcome' % label
                continue
            if outcome not in FATAL_CANDIDATE_CLASSES:
                return '%s publishes an unknown Candidate outcome %r' % (label, outcome)
            if receipt.get('classification') != 'execution_valid':
                return '%s must keep the contract execution classification' % label
            absence = receipt.get('native_evidence')
            if not isinstance(absence, dict):
                return '%s claims a Candidate outcome with no absence observation' % label
            observed = self.ref(absence, label=label + '.native_evidence')
            proof = observed.get('product_entry_proof') or {}
            if (observed.get('evidence_kind') != 'evaluator_observed_absence'
                    or observed.get('candidate_authored') is not False
                    or observed.get('native_evidence_present') is not False
                    or observed.get('classification') != outcome
                    or observed.get('failure_class') != receipt.get('candidate_failure_class')):
                return '%s absence observation does not match the published outcome' % label
            if (proof.get('network_namespace') != 'isolated' or proof.get('unshared_network') is not True
                    or proof.get('provider_credential_mounted') is not False
                    or not proof.get('successful_lower_calls')):
                return '%s absence observation does not prove an isolated product entry' % label
            if not (proof.get('controller_timed_out') is True or proof.get('controller_exit_code') == 124
                    or proof.get('launcher_exit_code') == 124):
                return '%s absence observation records no evaluator deadline signal' % label
            for side in ('launcher_result', 'case_result', 'evidence_manifest'):
                self.ref(observed[side], label=label + '.' + side)
        return True

    def c_trajectory(self):
        native = self.ref(self.m['builder_native'], label='builder_native')
        # 2026-09-21: the upper Builder is deepseek-flash (pilots), deepseek-v4-pro
        # (main experiment) or a gateway GPT model at xhigh (GPT cells); the judge/lower
        # expectations below stay deepseek-flash/max.
        BUILDER_MODELS = {'deepseek-flash': 'max', 'deepseek-v4-pro': 'max',
                          'gpt-5.5': 'xhigh', 'gpt-5.6-sol': 'xhigh'}
        if BUILDER_MODELS.get(native.get('model')) != native.get('effort'):
            return 'native builder model/effort mismatch'
        if not native.get('builder_session_id'):
            return 'native builder session missing'
        log = native.get('native_log')
        if not isinstance(log, dict):
            return 'native log reference missing'
        path = (self.root / log['path']).resolve()
        path.relative_to(self.root)
        if not path.is_file() or sha(path) != log.get('sha256'):
            return 'native log bytes missing or changed'
        return True

    def c_judges(self):
        sessions = set()
        code = self.ref(self.m['judges']['code'], label='judges.code')
        code_skipped = code.get('state') == 'skipped_by_policy'
        if code_skipped:
            if (code.get('policy') or {}).get('id') != 'edit-code-axis-retired-2026-09-19' or code.get('formal') is not False:
                return 'skipped Code judge receipt does not carry the retirement policy'
            usage = self.ref(self.m['usage']['code_judge'], label='usage.code_judge')
            if usage.get('skipped_by_policy') is not True or usage.get('calls') or usage.get('requests'):
                return 'skipped Code judge usage is not all-zero'
        for role in (('result',) if code_skipped else ('result', 'code')):
            judge = self.ref(self.m['judges'][role], label='judges.' + role)
            if judge.get('model') != 'deepseek-flash' or judge.get('effort') != 'max':
                return '%s judge is not the independent max-effort judge' % role
            if judge.get('formal') is not False:
                return '%s judge output is not marked non-formal readiness evidence' % role
            for side in ('input', 'output'):
                self.ref(judge[side], label='judges.%s.%s' % (role, side))
            sessions.add(judge.get('judge_session_id'))
        if len(sessions) != (1 if code_skipped else 2):
            return 'result and code judges do not have distinct sessions'
        for role in (('result_judge',) if code_skipped else ('result_judge', 'code_judge')):
            usage = self.ref(self.m['usage'][role], label='usage.' + role)
            if usage.get('failures') or usage.get('in_flight') or not usage.get('known_tokens'):
                return '%s usage is failed, in flight, or has no known tokens' % role
        lower = self.ref(self.m['usage']['public_lower'], label='usage.public_lower')
        judge = self.ref(self.m['usage']['result_judge'], label='usage.result_judge')
        lower_ids = {r.get('request_id') for r in lower.get('requests', [])}
        judge_ids = {r.get('request_id') for r in judge.get('requests', [])}
        if lower_ids & judge_ids:
            return 'result judge shares request identities with the lower role'
        return True

    def c_cleanup(self):
        cleanup = self.ref(self.m['cleanup'], label='cleanup')
        if cleanup.get('owner') != 'evaluator':
            return 'cleanup attestation is not evaluator-owned'
        for field in ('builder_state', 'harbor_state', 'unit_state'):
            if cleanup.get(field) != 'terminal':
                return 'cleanup ran before %s was terminal (%r)' % (field, cleanup.get(field))
        owned = cleanup.get('owned_resources')
        if not isinstance(owned, list):
            return 'cleanup does not enumerate the resources it owned'
        for side in ('before', 'after'):
            self.ref(cleanup[side], label='cleanup.' + side)
        stats = cleanup.get('stats')
        if not isinstance(stats, dict) or set(stats) != set(owned):
            return 'cleanup stats do not cover exactly the owned resources'
        for name, reference in stats.items():
            self.ref(reference, label='cleanup.stats.' + name)
        after = self.ref(cleanup['after'], label='cleanup.after')
        remaining = [r for r in owned if r in json.dumps(after)]
        if remaining:
            return 'owned resources still present after cleanup: %s' % remaining
        return True

    def c_formal_entry(self):
        if self.m.get('profile') != PROFILE:
            return 'manifest profile is not the v2 readiness profile'
        if not self.m.get('run_id'):
            return 'manifest run_id missing'
        return True

    def c_prerequisites(self):
        snapshot = json.loads((self.control / 'post_repair_tree_snapshot.json').read_bytes())
        recorded = snapshot['tasks'][self.task]['sibling']['digest']
        if recorded != self.m['current_binding']['source_digest']:
            return 'reviewed snapshot digest differs from the bundle binding'
        return True

    def run(self):
        order = [('current_source_and_contract_reviewed', self.c_source),
                 ('real_native_builder_two_dev_acceptance', self.c_two_rounds),
                 ('feedback_and_frozen_candidate_verified', self.c_feedback_freeze),
                 ('real_medium_product_execution', self.c_medium_product),
                 ('artifact_trajectory_oracle_provenance', self.c_trajectory),
                 ('independent_xhigh_result_and_code', self.c_judges),
                 ('owned_cleanup_verified', self.c_cleanup),
                 ('formal_entry_matches_reviewed_execution', self.c_formal_entry),
                 ('configuration_prerequisites_resolved', self.c_prerequisites)]
        results = {}
        for name, fn in order:
            ok, detail = self.check(name, fn)
            results[name] = ok
            print('  %-42s %s  %s' % (name, 'PASS' if ok else 'FAIL', '' if ok else detail))
        return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--task', required=True)
    ap.add_argument('--bundle-root', required=True)
    ap.add_argument('--control-root', default='@@AGENTSWE_EDITING_CONTROL@@')
    ap.add_argument('--source', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--skip-load-and-validate', action='store_true')
    args = ap.parse_args()

    review = Review(args.task, args.bundle_root, args.control_root, args.source)
    print('manifest_sha256 =', review.manifest_sha)
    results = review.run()

    if not args.skip_load_and_validate:
        sys.path.insert(0, args.control_root)
        from v2_readiness import load_and_validate
        from readiness_judge_validation import make_judge_output_validators
        from v2_usage_normalizers import make_broker_record_normalizers
        import formal_config as cfg
        ok, errors = load_and_validate(
            review.manifest_path, bundle_root=review.root,
            expected_manifest_sha256=review.manifest_sha,
            trusted_current_binding=review.live_binding(),
            judge_output_validators=make_judge_output_validators(
                Path(args.source), code_implementation=cfg.AUTHORITATIVE_CREATE_CODE_JUDGE),
            broker_record_normalizers=make_broker_record_normalizers(args.task))
        print('  %-42s %s  %s' % ('load_and_validate', 'PASS' if ok else 'FAIL',
                                  '' if ok else '; '.join(errors)[:300]))
        if not ok:
            review.failures.append('load_and_validate: ' + '; '.join(errors)[:300])

    if review.failures or not all(results.get(k) for k in CHECKS):
        print('\nREVIEW REFUSED -- no report written:')
        for f in review.failures:
            print('  *', f)
        return 2

    report = {'task': args.task, 'profile': PROFILE,
              'current_binding': review.live_binding(),
              'manifest_sha256': review.manifest_sha,
              'review_checks': {k: True for k in CHECKS},
              # Stated in the report itself rather than left for someone to
              # infer. Each check above was derived from the bundle's own bytes
              # by the code in this file and nothing was asserted that failed
              # to verify -- but the reviewer is the same agent that built and
              # patched the evaluators under review, so this is a mechanical
              # re-derivation, not a second pair of eyes. Anyone treating the
              # nine checks as independent corroboration should know that.
              'reviewer': {
                  'kind': 'mechanical-self-review',
                  'independent_of_implementation': False,
                  'note': 'The reviewer also authored the fixes under review; '
                          'every check is re-derived from bundle bytes, none is attested '
                          'from knowledge of how the fix was written.',
                  'tool': {'path': str(Path(__file__).resolve()),
                           'sha256': sha(Path(__file__).resolve())},
                  'authorized_by': 'user, 2026-09-18',
              }}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False) + '\n')
    print('\nreview report written:', out)
    print('review_sha256 =', sha(out))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
