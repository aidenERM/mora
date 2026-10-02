from datetime import datetime, timezone
import json
import time

from .tools import TOOLS, risk, READ_TOOLS
from .memory import Memory
from .security import clean
from .providers import ConfigurationError
from .policy import Policy
from urllib.error import URLError


class Engine:
    def __init__(self, store, router, tools):
        self.store, self.router, self.tools = store, router, tools
        self.memory = Memory(store)
        self.policy = Policy()

    def execute(self, task_id, stopping=None):
        errors = 0
        while True:
            step = None
            task = self.store.task(task_id)
            if task["status"] != "running":
                return task
            if stopping and stopping():
                self.store.change(task_id, "queued")
                return self.store.task(task_id)
            if task["steps"] >= task["max_steps"]:
                self.store.change(task_id, "failed", error="step_budget_exhausted")
                return self.store.task(task_id)
            history = self.store.history(task_id)
            approved = self.store.approved_step(task_id)
            try:
                if approved:
                    action, args = approved["action"], approved["arguments"]
                    step = approved["id"]
                    self.store.finish_step(step, {}, "started")
                else:
                    compact = [{"action": row["action"], "status": row["status"], "observation": json.dumps(row["observation"])[:3500]} for row in history[-6:]]
                    context = {"goal": task["goal"], "constraints": task["metadata"], "observations": compact,
                               "memory": self.memory.retrieve(task["goal"]),
                               "now": datetime.now(timezone.utc).isoformat(), "timezone": "America/Bogota",
                               "woke_from_schedule": bool(task["wake_at"] and task["wake_at"] <= time.time()),
                               "remaining_steps": task["max_steps"] - task["steps"]}
                    self.store.reserve_decision(task_id, task["max_steps"] + 2)
                    image = self.tools.vision(history[-1]["observation"]) if history and history[-1]["action"] == "image_inspect" and history[-1]["status"] == "observed" else None
                    decision, usage = self.router.decide(task, context, image) if image else self.router.decide(task, context)
                    if self.store.task(task_id)["status"] != "running":
                        return self.store.task(task_id)
                    if stopping and stopping():
                        self.store.change(task_id, "queued")
                        return self.store.task(task_id)
                    action = decision["action"]
                    args = clean(json.loads(decision["arguments"] or "{}"))
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be an object")
                    if action == "file_write" and args.get("overwrite") is True:
                        args = self.tools.prepare_write(args)
                    step = self.store.start_step(task_id, action, args)
                    self.store.state("last_provider", usage)
                    if usage.get("fallback_from"):
                        self.memory.notice(task_id, "fallback", "OpenAI failed. I’m continuing this task through AWS with the same context.")
                    if action == "finish":
                        proposed_checks = json.loads(decision["verification"] or "[]")
                        with self.store.connect() as db:
                            db.execute("UPDATE steps SET arguments=? WHERE id=?", (json.dumps(clean({"checks": proposed_checks})), step))
                        checks = self.tools.verify(proposed_checks)
                        self.store.finish_step(step, {"summary": decision["summary"], "checks": checks}, "verified")
                        self.store.change(task_id, "completed", result=decision["summary"])
                        self.store.state("report_requested:" + task_id, decision.get("notify") == "yes")
                        return self.store.task(task_id)
                    if action == "block":
                        self.store.finish_step(step, {"reason": decision["summary"]})
                        self.store.change(task_id, "blocked", result=decision["summary"], error="needs_external_input")
                        return self.store.task(task_id)
                    if action == "wait":
                        wake = datetime.fromisoformat(decision["wake_at"].replace("Z", "+00:00"))
                        if wake.tzinfo is None or not time.time() < wake.timestamp() <= time.time() + 365 * 86400:
                            raise ValueError("future timezone-aware wake time required")
                        self.store.finish_step(step, {"reason": decision["summary"], "wake_at": wake.isoformat()})
                        self.store.change(task_id, "waiting", result=decision["summary"], wake_at=wake.timestamp())
                        return self.store.task(task_id)
                    repeated = [row for row in history[-3:] if row["action"] == action and row["arguments"] == args]
                    if len(repeated) >= 3:
                        raise ValueError("repeated action without progress")
                    classification = self.policy.classify(action, args, task["goal"])
                    self.store.state("risk_decision:" + str(step), classification)
                    reason = classification["reason"]
                    if task["metadata"].get("read_only") and action not in READ_TOOLS and action != "browser_navigate":
                        reason = "task is restricted to read-only investigation"
                        self.store.state("risk_decision:" + str(step), {"level": "approval_required", "requires_approval": True, "reason": reason})
                    if reason:
                        args = self.tools.bind_approval(action, args)
                        self.policy.evaluate(action, args, task["goal"])
                        with self.store.connect() as db:
                            db.execute("UPDATE steps SET arguments=? WHERE id=?", (json.dumps(clean(args)), step))
                        self.store.require_approval(step, task_id, reason)
                        return self.store.task(task_id)
                result = self.tools.invoke(action, args, step)
                if action == "shell" and not result.get("ok"):
                    raise ValueError("shell command failed or timed out")
                self.store.finish_step(step, result)
                errors = 0
            except ConfigurationError as exc:
                message = "Vision is not configured. The image is stored privately; configure a supported vision model to continue." if str(exc).startswith("vision") else "OpenAI key/model is not configured. Select aws to continue through the existing connection."
                self.store.change(task_id, "blocked", result=message, error="provider_not_configured")
                return self.store.task(task_id)
            except Exception as exc:
                # Never persist provider exception text, which may contain request data.
                if step is not None:
                    detail = str(exc)[:240] if type(exc) is ValueError else ""
                    self.store.finish_step(step, {"error": type(exc).__name__, "detail": detail}, "failed")
                else:
                    step = self.store.start_step(task_id, "provider_error", {})
                    self.store.finish_step(step, {"error": type(exc).__name__}, "failed")
                if isinstance(exc, URLError) and approved:
                    self.store.change(task_id, "blocked", result="The approved action has an uncertain outcome. Inspect current state before retrying.", error="tool_outcome_unknown")
                    return self.store.task(task_id)
                errors += 1
                if errors >= 2:
                    self.store.change(task_id, "failed", error=type(exc).__name__)
                    return self.store.task(task_id)
                time.sleep(0.2)
