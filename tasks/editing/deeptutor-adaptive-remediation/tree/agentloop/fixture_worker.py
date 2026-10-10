"""Generic isolated product RPC. Contains no case inventory, oracle or answers.

Operations only seed recorded historical learner activity, call public tools,
and read projections. The trusted controller selects preconditions outside this
namespace; post-execution reads use a disposable home, never live agent state.
"""
from __future__ import annotations
import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import socket
import sys
import time


async def dispatch(method, args):
    if method == "boundary_probe":
        visible = {path: Path(path).exists() for path in args.get("paths", [])}
        reachable = False
        try:
            with socket.create_connection(("127.0.0.1", int(args["host_port"])), timeout=.2):
                reachable = True
        except OSError:
            pass
        return {"visible": visible, "host_tcp_reachable": reachable,
                "inherited_secret": "AGENTSWE_TEST_PRIVATE_CANARY" in os.environ}
    if method == "create_session":
        from deeptutor.services.session import get_session_store
        return await get_session_store().create_session(**args)
    if method == "tool_names" or method == "tool_call":
        from deeptutor.capabilities.mastery.tools import MASTERY_TOOL_TYPES
        registry = {tool.get_definition().name: tool for tool in [kind() for kind in MASTERY_TOOL_TYPES]}
        if method == "tool_names":
            return list(registry)
        result = await registry[args["name"]].execute(**args["arguments"])
        return {"success": bool(result.success), "content": str(result.content)}
    if method == "learner_projection":
        from deeptutor.learning.storage import LearningStore
        progress = LearningStore().load(args["path_id"])
        if progress is None:
            return {"path_missing": True}
        value = progress.model_dump(mode="json")
        for key in ("updated_at", "last_accessed_at"):
            value.pop(key, None)
        return value
    if method == "seed_learner":
        from deeptutor.learning.models import ErrorRecord, ErrorType, KnowledgePoint, KnowledgeType, LearningModule, LearningStage
        from deeptutor.learning.service import LearningService
        from deeptutor.learning.storage import LearningStore
        path_id, nonce = args["path_id"], args["nonce"]
        service = LearningService(LearningStore())
        progress = service.get_or_create(path_id)
        module_id, kp_id = "signed_numbers_" + nonce, "negative_product_" + nonce
        if not progress.modules:
            service.replace_modules(progress, [LearningModule(id=module_id, name="Signed-number multiplication", order=0,
                knowledge_points=[KnowledgePoint(id=kp_id, name="Multiply two negative integers", type=KnowledgeType.PROCEDURE, module_id=module_id)])])
            progress.current_module_id, progress.current_stage = module_id, LearningStage.REVIEW
        record_id = ("new_error_" if args.get("additional_error") else "error_") + nonce
        if not any(row.id == record_id for row in progress.error_records):
            progress.error_records.append(ErrorRecord(id=record_id, question_id="q_" + record_id, knowledge_point_id=kp_id,
                module_id=module_id, error_type=ErrorType.UNDERSTANDING_DEVIATION,
                self_attribution="I kept the negative sign when multiplying -3 by -4.",
                ai_confirmation="A completed earlier diagnostic recorded -12; the product's expected answer was 12.",
                status="active", created_at=time.time() - 120))
            service.save(progress)
        return {"path_id": path_id, "module_id": module_id, "knowledge_point_id": kp_id, "error_record_id": record_id}
    if method == "graduate_history":
        from deeptutor.learning.models import QuizAttempt
        from deeptutor.learning.service import LearningService
        from deeptutor.learning.storage import LearningStore
        service = LearningService(LearningStore())
        progress = service.get_or_create(args["path_id"])
        for error in progress.error_records:
            error.status = "graduated"
        for i in range(4):
            progress.quiz_attempts.append(QuizAttempt(question_id=f"subsequent_{i}_{args['nonce']}",
                knowledge_point_id=args["knowledge_point_id"], module_id=args["module_id"],
                is_correct=True, user_answer="12", mastery_estimate=.95, timestamp=args["at"] + 5 + i))
        progress.mastery_levels[args["knowledge_point_id"]] = .95
        service.save(progress)
        return {"recorded_correct_attempts": 4}
    raise ValueError("unsupported isolated product operation")


async def serve():
    wire = sys.stdout
    for line in sys.stdin:
        request = json.loads(line)
        try:
            with contextlib.redirect_stdout(sys.stderr):
                value = await dispatch(request["method"], request.get("arguments", {}))
            reply = {"id": request["id"], "ok": True, "value": value}
        except Exception as exc:
            reply = {"id": request["id"], "ok": False, "error_type": type(exc).__name__, "reason": str(exc)[:1000]}
        wire.write(json.dumps(reply, default=str) + "\n")
        wire.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    options = parser.parse_args()
    # This file starts with -I inside the namespace. Only now may product code
    # enter the interpreter's import path; host sitecustomize never runs.
    sys.path.insert(0, str(options.repository))
    asyncio.run(serve())
