"""Offline count of the unmodified Create Code prompt; never read credentials."""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
import runpy
import socket
from pathlib import Path
import tiktoken


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--scope', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--requirements', type=Path, required=True)
    parser.add_argument('--rubric', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prepare-task-root', type=Path)
    parser.add_argument('--candidate-digest')
    args = parser.parse_args()
    if args.prepare_task_root:
        planner = runpy.run_path(str(args.prepare_task_root / 'evaluator/code_evidence_scope.py'))
        if not args.candidate_digest:
            parser.error('--candidate-digest is required for exact input preparation')
        scope = planner['prepare_code_evidence_scope'](args.candidate, args.candidate_digest, args.scope.parent)
        shared = runpy.run_path('@@AGENTSWE_EDITING_CONTROL@@/formal_axes_shared.py',
            init_globals={'ROOT_OVERRIDE': args.prepare_task_root})
        shared['checked_code_scope'](scope, args.candidate, args.candidate_digest)
        if args.requirements.exists():
            raise RuntimeError('new requirements output required; never overwrite old Code evidence')
        shared['public_requirements'](args.requirements)
        (args.requirements / 'evaluator_scope_context.md').write_text(shared['code_scope_context_text'](scope))
    source = Path('@@AGENTSWE_EDITING_CONTROL@@/code_eval.py')
    code = source.read_text()
    create = runpy.run_path(str(source), run_name='code_prompt_offline')
    scope = json.loads(args.scope.read_text())
    _, source_pack = create['source_manifest_and_pack'](args.candidate,
        max_pack_bytes=create['MAX_SOURCE_PACK_BYTES'], evidence_paths=scope['evidence_paths'])
    _, requirements_pack = create['source_manifest_and_pack'](args.requirements,
        max_pack_bytes=create['MAX_REQUIREMENTS_PACK_BYTES'])
    # Evaluate only the trusted Create prompt expression, not main(), its env
    # loader, transport, container, provider or grading lifecycle.
    main_node = next(node for node in ast.parse(code).body if isinstance(node, ast.FunctionDef) and node.name == 'main')
    assignment = next(node for node in ast.walk(main_node) if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == 'prompt' for target in node.targets))
    namespace = dict(create, source_pack=source_pack, requirements_pack=requirements_pack,
        rubric_text=args.rubric.read_text())
    prompt = eval(compile(ast.Expression(assignment.value), str(source), 'eval'), namespace)
    original_socket = socket.socket
    def denied(*_args, **_kwargs):
        raise RuntimeError('Offline tokenizer cache missing; network disabled for this verification')
    socket.socket = denied
    try:
        encoding = tiktoken.get_encoding('o200k_base')
        values = {'source_pack_tokens': len(encoding.encode(source_pack, disallowed_special=())),
            'requirements_pack_tokens': len(encoding.encode(requirements_pack, disallowed_special=())),
            'full_prompt_tokens': len(encoding.encode(prompt, disallowed_special=()))}
    finally:
        socket.socket = original_socket
    output = {'schema_version': 'agentswe-offline-code-prompt-tokens/v1',
        'tokenizer': 'tiktoken/o200k_base', 'tokenizer_version': tiktoken.__version__,
        'model_tokenizer_mapping_verified': False,
        'mapping_note': 'GATEWAY alias gpt-5.6-sol does not establish a published tokenizer/context limit; o200k_base is an explicit measured encoding, not a guarantee of capacity.',
        'candidate_digest': scope['candidate_digest'], 'scope_manifest_sha256': hashlib.sha256(args.scope.read_bytes()).hexdigest(),
        'source_pack_bytes': len(source_pack.encode()), 'prompt_bytes': len(prompt.encode()),
        'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
        'requirements_root': str(args.requirements), 'rubric': str(args.rubric),
        'network_calls': 0, 'provider_calls': 0, **values}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + '\n')
    print(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()
