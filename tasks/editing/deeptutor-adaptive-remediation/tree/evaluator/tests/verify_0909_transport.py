"""Actual product/sandbox with an explicitly mocked provider, no paid calls."""
import argparse
import http.server
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from agentloop.lower_agent_entry import run, _sandbox_command, _failure_classification
from agentloop.lower_transport import UnixHTTPRelay
from agentloop.execution_evidence import artifact_authorship

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--through-broker",action="store_true")
    parser.add_argument("--repository",type=Path,required=True)
    parser.add_argument("--python",required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args(); args.output.mkdir(parents=True,exist_ok=False)
    candidate=args.output/"candidate"
    shutil.copytree(args.repository,candidate)
    for p in (candidate,*candidate.rglob("*")): p.chmod(p.stat().st_mode|0o600|(0o100 if p.is_dir() else 0))
    requests=[]; reject=[False]
    artifact={"schema_version":"v1","case_id":"test_001","status":"incomplete",
        "summary":"Offline mocked-provider transport test; not a benchmark result.","artifacts":{"mocked_provider":True}}
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*a): pass
        def do_POST(self):
            value=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path":self.path,"body":value})
            if reject[0] or self.path!="/v1/responses":
                self.send_response(404); self.end_headers(); self.wfile.write(b'{"error":{"type":"unsupported_endpoint"}}'); return
            if len(requests)==1:
                output=[{"type":"function_call","call_id":"call_mock_transport_1","name":"mastery_remediation_status","arguments":"{}"}]
            else:
                output=[{"type":"message","role":"assistant","content":[{"type":"output_text","text":json.dumps(artifact)}]}]
            raw=json.dumps({"id":"resp_mock_transport","status":"completed","output":output,
                "usage":{"input_tokens":10,"output_tokens":10,"total_tokens":20}}).encode()
            self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    server=http.server.ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    endpoint=f"http://127.0.0.1:{server.server_port}/v1/responses"
    broker_server=None
    if args.through_broker:
        from agentloop.broker import BrokerState,make_handler
        state=BrokerState(args.output/'broker-stats.json',0,0)
        broker_server=http.server.ThreadingHTTPServer(('127.0.0.1',0),make_handler(state,endpoint,'synthetic-only'))
        threading.Thread(target=broker_server.serve_forever,daemon=True).start()
        endpoint=f'http://127.0.0.1:{broker_server.server_port}/v1/responses'
    try:
        result=run(args.output/"scoped-candidate",ROOT/"test_cases/test_001/input.md",args.output/"test_001",broker_endpoint=endpoint,execute=True,python_executable=args.python,source_repository=candidate)
        checks={"source_copy_inside_case_scope": "agentswe-edit-owner-c-" in json.loads((args.output/"test_001/source-copy-observation.json").read_text())["cgroup"],
            "actual_product_uses_responses_endpoint":len(requests)>=2 and all(r["path"]=="/v1/responses" for r in requests),
            "actual_product_uses_locked_model":bool(requests) and all(r["body"].get("model")=="gpt-5.6-sol" and r["body"].get("reasoning",{}).get("effort")=="high" for r in requests),
            "product_client_factory_preflight":result.get("transport_preflight",{}).get("valid") is True,
            "actual_session_is_seeded_learner":result.get("observed_product_session_ids")==[result.get("bound_mastery_path_id")],
            "product_authored_artifact":artifact_authorship(args.output/"test_001","test_001")["valid"],
            "wrong_endpoint_is_infrastructure":_failure_classification("","unsupported_endpoint")=="evaluator_infrastructure_error"}
        # Probe the same network namespace against a live local mock listener:
        # a successful host TCP request here would be an isolation violation.
        runtime=args.output/"network_probe_runtime"; runtime.mkdir()
        script="import socket,json; s=socket.socket(); s.settimeout(1); result=s.connect_ex(('127.0.0.1',"+str(server.server_port)+")); print(json.dumps({'host_tcp_blocked':result!=0}))"
        command,_=_sandbox_command([args.python,"-c",script],candidate,runtime,args.python)
        probe=subprocess.run(command,text=True,capture_output=True,timeout=15,env={"PATH":"/usr/bin:/bin","PYTHONDONTWRITEBYTECODE":"1"})
        checks["host_tcp_is_inaccessible"]=probe.returncode==0 and json.loads(probe.stdout)["host_tcp_blocked"]
        reject[0]=True
        bad=run(candidate,ROOT/"test_cases/test_001/input.md",args.output/"endpoint_rejected/test_001",broker_endpoint=endpoint,execute=True,python_executable=args.python)
        checks["actual_broker_404_is_infrastructure"]=bad.get("classification")=="evaluator_infrastructure_error"
        broken=args.output/"broken_transport_runtime"; broken.mkdir()
        for name in ("sitecustomize.py","transport_bootstrap.py"):
            shutil.copyfile(ROOT/"agentloop"/name,broken/name)
        broken_marker=broken/"preflight.json"
        command,_=_sandbox_command([args.python,str(broken/"transport_bootstrap.py"),"--transport-probe-only"],candidate,broken,args.python)
        missing=subprocess.run(command,text=True,capture_output=True,timeout=30,env={"PATH":"/usr/bin:/bin",
            "PYTHONPATH":str(broken)+":"+str(candidate),"PYTHONDONTWRITEBYTECODE":"1",
            "AGENTSWE_RESPONSES_ADAPTER":"1","AGENTSWE_RESPONSES_BASE_URL":endpoint,
            "AGENTSWE_TRANSPORT_PREFLIGHT":str(broken_marker)})
        checks["missing_adapter_fails_before_product_api"]=missing.returncode==78 and broken_marker.is_file() and json.loads(broken_marker.read_text()).get("valid") is False
        if broker_server:
            ledger=state.public()
            checks['real_broker_single_attempt_ledger']=ledger['successful_calls']>=2 and all(row['transport_attempts']==1 for row in ledger['requests'])
            checks['real_broker_context_bound']=bool(ledger['logical_requests']) and all(row.get('context_id') for row in ledger['logical_requests'].values())
        report={"passed":all(checks.values()),"checks":checks,"mock_requests":len(requests),"provider_calls":0,
            "classification":result.get("classification"),"error":result.get("error"),"result":result}
        (args.output/"verification.json").write_text(json.dumps(report,indent=2))
        print(json.dumps({k:v for k,v in report.items() if k!="result"}))
        return 0 if report["passed"] else 2
    finally:
        if broker_server:broker_server.shutdown();broker_server.server_close()
        server.shutdown();server.server_close();thread.join(timeout=5)

if __name__=="__main__": raise SystemExit(main())
