import fcntl
import os
from pathlib import Path
import signal
import threading
import time

from .engine import Engine
from .providers import Router
from .store import Store
from .tools import Tools


def run_once(store=None, router=None, tools=None):
    store = store or Store()
    task = store.claim()
    if not task:
        return {"idle": True}
    tools = tools or Tools(Path(store.path).parent / "artifacts")
    return Engine(store, router or Router(), tools).execute(task["id"])


def main():
    os.umask(0o077)
    store = Store()
    lock = open(Path(store.path).parent / "worker.lock", "a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    store.recover()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    router, tools = Router(), Tools(Path(store.path).parent / "artifacts")
    while not stop.is_set():
        try:
            result = run_once(store, router, tools)
            store.state("worker", {"updated_at": time.time(), "ok": True})
        except Exception as exc:
            store.state("worker", {"updated_at": time.time(), "ok": False, "error": type(exc).__name__})
        stop.wait(2)


if __name__ == "__main__":
    main()
