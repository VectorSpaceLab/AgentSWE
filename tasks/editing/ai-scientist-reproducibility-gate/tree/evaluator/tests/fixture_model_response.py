"""Synthetic response capture for unit tests, never a benchmark reference Agent."""
import hashlib
import json
from pathlib import Path


def capture_artifact(value, response_capture):
    """Model the capture side effect; keep supplied claims unchanged."""
    path = Path(response_capture)
    assert path.name == 'model_final_response.txt'
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')
    with path.open('xb') as handle:
        handle.write(raw)
    return value, hashlib.sha256(raw).hexdigest()
