"""Persist maintenance state to selected cloud storage; use existing local PostgreSQL."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db import Connection, add_connection_arguments
from agent_state.engine import Engine
from agent_state.runtime import Runtime
from agent_state.config import load_settings, create_store
from agent_state.maintenance import SnapshotMaintenance
from agent_state.client import StateClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    state = parser.add_mutually_exclusive_group(required=True)
    state.add_argument("--cloud-config", type=Path, help="Connect directly to object storage")
    state.add_argument("--state-url", help="Remote state API, e.g. http://127.0.0.1:18844 over SSH")
    parser.add_argument("--task", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--table-id", required=True)
    parser.add_argument("--policy-ref")
    args = parser.parse_args()
    if args.state_url:
        runtime = StateClient(args.state_url)
    else:
        settings = load_settings(args.cloud_config)
        store = create_store(settings)
        store.check()
        runtime = Runtime(Engine(store, settings["prefix"]))
    runner = SnapshotMaintenance(runtime, Connection(args))
    print(json.dumps(runner.run(args.task, args.actor, args.revision, args.table_id, args.policy_ref), indent=2))


if __name__ == "__main__":
    main()
