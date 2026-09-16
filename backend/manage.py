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
        with store.connect() as conn:
            row = conn.execute("SELECT payload FROM decisions WHERE id=?", (args.decision_id,)).fetchone()
        if not row:
            parser.error("Decision does not exist")
        version = json.loads(row[0]).get("strategy_version", "")
        if version.startswith("phase3a-"):
            from .phase3a import Phase3AEngine
            engine = Phase3AEngine(store)
        elif version.startswith("phase2-"):
            from .phase2 import Phase2Engine
            engine = Phase2Engine(store)
        else:
            engine = Engine(store)
        result = engine.replay(args.decision_id)
        print(json.dumps(result, indent=2))
        if not result["matches"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
