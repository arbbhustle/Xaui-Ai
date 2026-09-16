"""Local-only maintenance: backup and deterministic decision replay."""
import argparse
import json
from pathlib import Path

from .engine import Engine
from .storage import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    replay = actions.add_parser("replay")
    replay.add_argument("decision_id")
    backup = actions.add_parser("backup")
    backup.add_argument("destination")
    args = parser.parse_args()
    if not Path(args.database).is_file():
        parser.error("Database does not exist")
    store = Store(args.database)
    if args.action == "backup":
        if Path(args.destination).exists():
            parser.error("Refusing to overwrite an existing backup")
        store.backup(args.destination)
        print("Backup complete")
    else:
        result = Engine(store).replay(args.decision_id)
        print(json.dumps(result, indent=2))
        if not result["matches"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
