"""Revalidate readiness judge payloads using the actual shared judge schemas.

Only imports parsers/validators; never dispatches a provider request. The source
SHA and rubric dimensions are checked against current evaluator-owned files.
"""
from pathlib import Path
import json
import importlib.util
import hashlib
import result_judge


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_judge_output_validators(task_source, *, code_implementation):
    source=Path(task_source).resolve(strict=True)
    code_path=Path(code_implementation)
    if code_path.is_symlink() or not code_path.is_file():
        raise ValueError('authoritative Code schema unavailable')
    spec=importlib.util.spec_from_file_location('readiness_authoritative_code',code_path)
    code=importlib.util.module_from_spec(spec);spec.loader.exec_module(code)
    result_sha=sha(Path(result_judge.__file__))
    code_sha=sha(code_path)

    def result(payload, inputs):
        if inputs.get('validator_source_sha256')!=result_sha:
            return ['Result validator source binding mismatch']
        relative=inputs.get('rubric_relative_path')
        if not isinstance(relative,str) or Path(relative).is_absolute() or '..' in Path(relative).parts:
            return ['Result rubric path must be within current task source']
        rubric=source/relative
        try:
            rubric.resolve(strict=True).relative_to(source)
            if not rubric.is_file() or rubric.is_symlink():
                return ['Result rubric is not a regular source file']
            dimensions=result_judge.load_dimensions(result_judge.rubric_dimensions_path(rubric))
            if 'dimension_maxima' not in inputs or inputs['dimension_maxima']!=dimensions:
                return ['Result schema dimensions differ from current task rubric']
            _,errors=result_judge.validate_response(json.dumps(payload),'test_001',dimensions)
            return errors
        except (OSError,ValueError,TypeError,AttributeError) as exc:
            return ['Result schema error: '+str(exc)]

    def validate_code(payload, inputs):
        if inputs.get('validator_source_sha256')!=code_sha:
            return ['Code validator source binding mismatch']
        manifest=inputs.get('source_manifest')
        if not isinstance(manifest,dict):
            return ['Code source manifest required']
        try:
            _,errors=code.validate_model_result(payload,manifest)
            return errors
        except (ValueError,TypeError,KeyError,AttributeError) as exc:
            return ['Code schema error: '+str(exc)]
    return {'result':result,'code':validate_code}
