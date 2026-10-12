"""Zero-network exact Create prompt packing for AI Scientist's full source."""
import argparse
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import runpy
import socket
import sys
import tiktoken

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'agentloop'))
from protocol import tree_digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--candidate-digest', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert tree_digest(args.candidate) == args.candidate_digest
    args.output.mkdir(parents=True, exist_ok=False)
    shared_path = Path('@@AGENTSWE_EDITING_CONTROL@@/formal_axes_shared.py')
    spec = importlib.util.spec_from_file_location('ai_code_full_token_requirements', shared_path)
    shared = importlib.util.module_from_spec(spec); sys.modules[spec.name] = shared; spec.loader.exec_module(shared)
    shared.ROOT = ROOT
    requirements = shared.public_requirements(args.output / 'public_requirements')
    create_path = Path('@@AGENTSWE_EDITING_CONTROL@@/code_eval.py')
    code = create_path.read_text()
    create = runpy.run_path(str(create_path), run_name='ai_code_tokens_offline')
    source_manifest, source_pack = create['source_manifest_and_pack'](args.candidate, max_pack_bytes=create['MAX_SOURCE_PACK_BYTES'])
    requirements_manifest, requirements_pack = create['source_manifest_and_pack'](requirements, max_pack_bytes=create['MAX_REQUIREMENTS_PACK_BYTES'])
    node = next(value for value in ast.parse(code).body if isinstance(value, ast.FunctionDef) and value.name == 'main')
    expression = next(value for value in ast.walk(node) if isinstance(value, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'prompt' for target in value.targets))
    rubric = ROOT / 'evaluator/code_rubric.md'
    namespace = dict(create, source_pack=source_pack, requirements_pack=requirements_pack, rubric_text=rubric.read_text())
    prompt = eval(compile(ast.Expression(expression.value), str(create_path), 'eval'), namespace)
    original = socket.socket
    def denied(*_args, **_kwargs): raise RuntimeError('Offline tokenizer unavailable; network denied')
    socket.socket = denied
    try:
        encoding = tiktoken.get_encoding('o200k_base')
        counts = {name + '_tokens': len(encoding.encode(value, disallowed_special=())) for name, value in (('source_pack', source_pack), ('requirements_pack', requirements_pack), ('full_prompt', prompt))}
    finally:
        socket.socket = original
    report = {'schema_version': 'agentswe-offline-code-prompt-tokens/v1', 'candidate_digest': args.candidate_digest,
        'selection_policy': 'unmodified authoritative Create full-source pack; no task scope or source exclusion',
        'source_manifest': source_manifest, 'requirements_manifest': requirements_manifest,
        'source_pack_bytes': len(source_pack.encode()), 'requirements_pack_bytes': len(requirements_pack.encode()),
        'prompt_bytes': len(prompt.encode()), 'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
        'public_requirements': str(requirements), 'rubric': str(rubric),
        'implementation_hashes': {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (Path(__file__), create_path, shared_path, rubric)},
        'tokenizer': 'tiktoken/o200k_base', 'tokenizer_version': tiktoken.__version__,
        'model_tokenizer_mapping_verified': False,
        'mapping_note': 'GATEWAY deepseek-flash alias does not verify a published tokenizer or context limit; explicit o200k_base counts do not guarantee provider capacity.',
        'network_calls': 0, 'provider_calls': 0, **counts}
    (args.output / 'token_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key not in {'source_manifest', 'requirements_manifest'}}, indent=2))


if __name__ == '__main__': main()
