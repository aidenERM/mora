from datetime import datetime, timezone
import json
import time

from .tools import TOOLS, risk, READ_TOOLS


class Engine:
    def __init__(self, store, router, tools):
        self.store, self.router, self.tools = store, router, tools

    def execute(self, task_id):
        errors = 0
        while True:
            step = None
            task = self.store.task(task_id)
            if task["status"] != "running":
                return task
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
                               "remaining_steps": task["max_steps"] - task["steps"]}
                    decision, usage = self.router.decide(task, context)
                    if self.store.task(task_id)["status"] != "running":
                        return self.store.task(task_id)
                    action = decision["action"]
                    args = json.loads(decision["arguments"] or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be an object")
                    step = self.store.start_step(task_id, action, args)
                    self.store.state("last_provider", usage)
                    if action == "finish":
                        proposed_checks = json.loads(decision["verification"] or "[]")
                        with self.store.connect() as db:
                            db.execute("UPDATE steps SET arguments=? WHERE id=?", (json.dumps({"checks": proposed_checks}), step))
                        checks = self.tools.verify(proposed_checks)
                        self.store.finish_step(step, {"summary": decision["summary"], "checks": checks}, "verified")
                        self.store.change(task_id, "completed", result=decision["summary"])
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
                    reason = risk(action, args)
                    if task["metadata"].get("read_only") and action not in READ_TOOLS and action != "browser_navigate":
                        reason = "task is restricted to read-only investigation"
                    if reason:
                        self.store.require_approval(step, task_id, reason)
                        return self.store.task(task_id)
                result = self.tools.invoke(action, args, step)
                if action == "shell" and not result.get("ok"):
                    raise ValueError("shell command failed or timed out")
                self.store.finish_step(step, result)
                errors = 0
            except Exception as exc:
                # Never persist provider exception text, which may contain request data.
                if step is not None:
                    detail = str(exc)[:240] if type(exc) is ValueError else ""
                    self.store.finish_step(step, {"error": type(exc).__name__, "detail": detail}, "failed")
                else:
                    step = self.store.start_step(task_id, "provider_error", {})
                    self.store.finish_step(step, {"error": type(exc).__name__}, "failed")
                errors += 1
                if errors >= 2:
                    self.store.change(task_id, "failed", error=type(exc).__name__)
                    return self.store.task(task_id)
                time.sleep(0.2)
