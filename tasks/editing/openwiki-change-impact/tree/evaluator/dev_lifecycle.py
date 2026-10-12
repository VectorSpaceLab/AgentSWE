from pathlib import Path
import runpy
_implementation = Path("@@AGENTSWE_EDITING_CONTROL@@/dev_lifecycle_shared.py")
if not _implementation.is_file():
    raise RuntimeError(f"missing evaluator-owned shared lifecycle implementation: {_implementation}")
globals().update(runpy.run_path(str(_implementation), run_name="openwiki_dev_lifecycle"))
