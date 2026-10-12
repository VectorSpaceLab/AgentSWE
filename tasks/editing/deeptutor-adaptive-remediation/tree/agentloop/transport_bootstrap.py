"""Fail-closed adapter gate, then invoke the unmodified product CLI entry."""
import json
import os
from pathlib import Path
import runpy
import sys

def main():
    marker=Path(os.environ["AGENTSWE_TRANSPORT_PREFLIGHT"])
    try:
        import sitecustomize
        if os.environ.get("AGENTSWE_CHAT_TO_RESPONSES_ADAPTER") != "installed":
            raise RuntimeError("Responses adapter failed to install: "+os.environ.get("AGENTSWE_CHAT_TO_RESPONSES_ADAPTER_ERROR","unknown"))
        from deeptutor.core.agentic import client
        from responses_client_adapter import ResponsesChatAdapter, transport_health
        if not isinstance(client.build_openai_client(None),ResponsesChatAdapter):
            raise RuntimeError("actual product client factory is not using evaluator Responses adapter")
        health=transport_health()
        marker.write_text(json.dumps({"valid":True,"product_factory_verified":True,"relay":health}))
    except Exception as exc:
        marker.write_text(json.dumps({"valid":False,"classification":"evaluator_infrastructure_error","reason":f"{type(exc).__name__}: {exc}"}))
        print("evaluator_transport_preflight_failure",file=sys.stderr)
        return 78
    if sys.argv[1:] == ["--transport-probe-only"]: return 0
    if sys.argv[1:3] != ["-m","deeptutor_cli"]: raise RuntimeError("unsupported product entry")
    sys.argv=["deeptutor_cli",*sys.argv[3:]]
    runpy.run_module("deeptutor_cli",run_name="__main__")
    return 0

if __name__=="__main__": raise SystemExit(main())
