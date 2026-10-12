#!/usr/bin/env python3
"""Run one official τ³ text-domain episode and emit a redacted native result."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import litellm

from tau2.agent.llm_agent import AGENT_INSTRUCTION, LLMAgent, SYSTEM_PROMPT
from tau2.data_model.simulation import TextRunConfig, TerminationReason
from tau2.evaluator.evaluator import EvaluationType
from tau2.registry import registry
from tau2.run import get_tasks, run_single_task


MODEL = "openai/responses/deepseek-flash"
ORDINARY_LOW_TERMINATIONS = {
    TerminationReason.MAX_STEPS,
    TerminationReason.TIMEOUT,
    TerminationReason.TOO_MANY_ERRORS,
    TerminationReason.AGENT_ERROR,
    TerminationReason.USER_ERROR,
    TerminationReason.CONTEXT_WINDOW_EXCEEDED,
    TerminationReason.UNEXPECTED_ERROR,
}


def classify_termination(
    reason: TerminationReason, *, reward_available: bool
) -> tuple[bool, bool, bool]:
    """Return (official, infrastructure_failure, force_low_score).

    The pinned upstream metrics exclude only ``INFRASTRUCTURE_ERROR``. In
    particular, exhausting max_steps is an evaluated agent outcome. Provider
    transport exceptions escape ``run_single_task`` (or are corroborated by
    broker failures in the controller) and remain infrastructure failures.
    """
    force_low_score = reason in ORDINARY_LOW_TERMINATIONS
    infrastructure_failure = reason is TerminationReason.INFRASTRUCTURE_ERROR
    official = not infrastructure_failure and (reward_available or force_low_score)
    return official, infrastructure_failure, force_low_score


class ConfigurableAgent(LLMAgent):
    def __init__(self, *args, strategy_prompt: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.strategy_prompt = strategy_prompt

    @property
    def system_prompt(self) -> str:
        instruction = AGENT_INSTRUCTION
        if self.strategy_prompt:
            instruction += "\n\n<builder_strategy>\n" + self.strategy_prompt + "\n</builder_strategy>"
        return SYSTEM_PROMPT.format(domain_policy=self.domain_policy, agent_instruction=instruction)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", choices=("airline", "retail"), required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--prediction", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=300)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    prediction = json.loads(args.prediction.read_text(encoding="utf-8"))
    agent_spec = prediction.get("agent") if isinstance(prediction, dict) else None
    if not isinstance(agent_spec, dict):
        raise SystemExit("prediction.agent missing")
    strategy = str(agent_spec.get("strategy_prompt", ""))
    if len(strategy) > 50000:
        raise SystemExit("strategy_prompt too large")

    def create_agent(tools, domain_policy, **kwargs):
        return ConfigurableAgent(
            tools=tools, domain_policy=domain_policy, strategy_prompt=strategy,
            llm=kwargs.get("llm"), llm_args=kwargs.get("llm_args"),
        )

    registry.register_agent_factory(create_agent, "agentswe_built_agent")
    litellm.drop_params = True
    config = TextRunConfig(
        domain=args.domain, task_ids=[args.task_id], num_trials=1,
        agent="agentswe_built_agent", llm_agent=MODEL,
        llm_args_agent={"temperature": 0.0, "timeout": 180, "num_retries": 0},
        user="user_simulator", llm_user=MODEL,
        llm_args_user={"temperature": 0.0, "timeout": 180, "num_retries": 0},
        max_steps=25, max_errors=10, timeout=900, max_concurrency=1,
        max_retries=0, seed=args.seed, enforce_communication_protocol=True,
        verbose_logs=False,
    )
    task = get_tasks(args.domain, task_ids=[args.task_id])[0]
    try:
        simulation = run_single_task(
            config, task, seed=args.seed, evaluation_type=EvaluationType.ALL,
            save_dir=None, verbose_logs=False,
        )
    except Exception as exc:
        result = {
            "schema_version": "1.0", "benchmark": f"tau3-{args.domain}-half-duplex",
            "case_id": args.case_id, "domain": args.domain,
            "task_id": args.task_id,
            "official_evaluation": False, "validity_gate": False,
            "infrastructure_failure": True, "score": 0,
            "errors": [f"native_runtime_{type(exc).__name__}"],
        }
        (args.output_dir / "native_result.json").write_text(json.dumps(result, indent=2) + "\n")
        return 0
    reward = simulation.reward_info
    termination = simulation.termination_reason.value
    # Match the official metrics boundary: only INFRASTRUCTURE_ERROR is
    # excluded. Budget exhaustion and agent-side failures remain evaluated.
    official, infrastructure_failure, force_low_score = classify_termination(
        simulation.termination_reason, reward_available=reward is not None
    )
    reward_value = float(reward.reward) if reward else 0.0
    if force_low_score:
        reward_value = 0.0
    messages = [message.model_dump(mode="json") for message in simulation.messages]
    tool_calls_per_message = [
        len(message.get("tool_calls") or [])
        for message in messages if isinstance(message, dict)
    ]
    max_tool_calls = max(tool_calls_per_message, default=0)
    tool_budget_exceeded = max_tool_calls > 4
    if tool_budget_exceeded:
        reward_value = 0.0
    (args.output_dir / "trajectory.json").write_text(json.dumps(messages, indent=2, ensure_ascii=False) + "\n")
    result = {
        "schema_version": "1.0", "benchmark": f"tau3-{args.domain}-half-duplex",
        "case_id": args.case_id, "domain": args.domain,
        "task_id": args.task_id,
        "official_evaluation": official, "validity_gate": official,
        "infrastructure_failure": infrastructure_failure,
        "score": round(100 * reward_value) if official else 0,
        "reward": reward_value, "termination_reason": termination,
        "ordinary_agent_failure": force_low_score,
        "message_count": len(messages),
        "max_tool_calls_in_turn": max_tool_calls,
        "tool_call_budget_per_turn": 4,
        "tool_budget_exceeded": tool_budget_exceeded,
        "trajectory_digest": hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest(),
        "reward_info": reward.model_dump(mode="json") if reward else None,
        "agent_usage": simulation.agent_usage.model_dump(mode="json") if simulation.agent_usage else None,
    }
    (args.output_dir / "native_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
