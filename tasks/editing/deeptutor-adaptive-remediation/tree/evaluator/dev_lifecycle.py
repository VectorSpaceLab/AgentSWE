from pathlib import Path
import runpy
_implementation = Path("@@AGENTSWE_EDITING_CONTROL@@/dev_lifecycle_shared.py")
globals().update(runpy.run_path(str(_implementation), run_name="deeptutor_dev_lifecycle"))
