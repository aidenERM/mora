import argparse
import json

from .store import Store


def main():
    parser = argparse.ArgumentParser(prog="python -m orbit")
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("goal")
    submit.add_argument("--model", default="auto")
    submit.add_argument("--steps", type=int, default=12)
    status = commands.add_parser("status")
    status.add_argument("id", nargs="?")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("id")
    resume = commands.add_parser("resume")
    resume.add_argument("id")
    selection = commands.add_parser("model")
    selection.add_argument("id")
    selection.add_argument("selection")
    approval = commands.add_parser("approve")
    approval.add_argument("step_id", type=int)
    approval.add_argument("--deny", action="store_true")
    commands.add_parser("once")
    args = parser.parse_args()
    store = Store()
    if args.command == "submit":
        result = store.create(args.goal, args.model, args.steps)
    elif args.command == "status":
        result = {"task": store.task(args.id), "steps": store.history(args.id)} if args.id else store.list()
    elif args.command == "cancel":
        result = store.cancel(args.id)
    elif args.command == "resume":
        result = store.resume(args.id)
    elif args.command == "model":
        result = store.select_model(args.id, args.selection)
    elif args.command == "approve":
        store.approve(args.step_id, not args.deny)
        result = {"ok": True}
    else:
        from .worker import run_once
        result = run_once(store)
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
