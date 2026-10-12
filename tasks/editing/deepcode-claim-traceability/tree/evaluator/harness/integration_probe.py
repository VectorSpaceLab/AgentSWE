from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.repository.resolve()))
    from workflows.code_implementation_workflow import CodeImplementationWorkflow

    workflow = CodeImplementationWorkflow(require_verification=True)
    records = asyncio.run(workflow._verify_generated_code(args.project.resolve()))
    print("DEEPCODE_INTEGRATION_RESULT=" + json.dumps(records, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
