"""Opt-in bounded live verification using actual configured AWS and Cloud PC."""
import argparse
import json
from pathlib import Path
import uuid

from .engine import Engine
from .providers import ProviderError, Router
from .store import Store
from .tools import Tools


class FailedPrimary(Router):
    def choose(self, task):
        return "openai", "injected-unavailable-primary"

    def openai(self, *args, **kwargs):
        raise ProviderError("controlled_primary_outage")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-aws", action="store_true", required=True)
    parser.add_argument("--fallback-probe", action="store_true")
    args = parser.parse_args()
    marker = "Orbit verified " + uuid.uuid4().hex
    root = Path("/var/lib/pulse/orbit/verification")
    store = Store(root / "probe.sqlite3")
    store.recover()
    task = store.create(f"Create verification/{marker.split()[-1]}.txt containing exactly {marker}. Independently read back that exact content before finishing.", model="aws", max_steps=10, source="verification")
    claimed = store.claim()
    if not claimed or claimed["id"] != task["id"]:
        raise RuntimeError("verification store has another pending task")
    tools = Tools(root / "artifacts")
    result = Engine(store, FailedPrimary() if args.fallback_probe else Router(), tools).execute(task["id"])
    if result["status"] != "completed":
        print(json.dumps({"passed": False, "task_id": task["id"], "status": result["status"], "error": result["error"]}))
        raise SystemExit(1)
    read = tools.invoke("file_read", {"path": "verification/" + marker.split()[-1] + ".txt"}, "check")
    assert read["content"] == marker
    provider = store.state("last_provider")
    if args.fallback_probe:
        assert provider["provider"] == "aws" and provider["fallback_from"] == "openai"
    print(json.dumps({"passed": True, "real_aws": True, "real_cloudpc_file_verified": True,
                      "primary_failure_injected": args.fallback_probe, "provider": provider["provider"],
                      "steps": result["steps"]}))


if __name__ == "__main__":
    main()
