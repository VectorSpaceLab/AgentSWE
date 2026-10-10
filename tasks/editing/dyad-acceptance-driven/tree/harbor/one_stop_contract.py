from pathlib import Path
import runpy
_implementation = Path("@@AGENTSWE_EDITING_CONTROL@@/one_stop_contract_shared.py")
if not _implementation.is_file():
    raise RuntimeError(f"missing evaluator-owned shared one-stop contract: {_implementation}")
globals().update(runpy.run_path(str(_implementation), run_name="dyad_one_stop_contract"))
