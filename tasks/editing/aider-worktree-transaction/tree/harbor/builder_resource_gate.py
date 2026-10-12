"""Harbor healthcheck: verify resources before the unmodified native agent setup."""
import json
from pathlib import Path

base = Path('/sys/fs/cgroup')
observed = {name: (base / name).read_text().strip()
            for name in ('memory.max', 'cpu.max', 'cpuset.cpus.effective')}
valid = observed['memory.max'] == str(16 * 1024**3) and observed['cpu.max'] == '800000 100000'
value = {'schema_version': 'agentswe-builder-resource-healthcheck/v1',
         'valid': valid, 'observed': observed}
Path('/logs/agent/builder_resource_gate.json').write_text(json.dumps(value, indent=2) + '\n')
print(json.dumps(value))
raise SystemExit(0 if valid else 125)
