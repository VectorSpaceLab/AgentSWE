"""Trusted controller of a complete aggregate build scope."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import candidate_adapter
from protocol import write_json
p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--candidate',type=Path,required=True);p.add_argument('--build-dir',type=Path,required=True);p.add_argument('--no-build',action='store_true');p.add_argument('--readiness-source-check',action='store_true');a=p.parse_args()
candidate_adapter._BUILD_SCOPE=json.loads((a.build_dir/'build_resources/scope-ownership.json').read_text())
value=candidate_adapter._build_candidate(a.source,a.candidate,a.build_dir,not a.no_build,readiness_source_check=a.readiness_source_check)
write_json(a.build_dir/'owned-build-result.json',value)
print(json.dumps(value))
