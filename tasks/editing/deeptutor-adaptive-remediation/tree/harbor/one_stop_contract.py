from pathlib import Path
import runpy
_implementation = Path("@@AGENTSWE_EDITING_CONTROL@@/one_stop_contract_shared.py")
globals().update(runpy.run_path(str(_implementation), run_name="deeptutor_one_stop_contract"))
