from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .hub_config import HubConfigError, load_hub_config
from .pilot import run_codex_pilot
from .registry import RegistryError, load_registry
from .service import ProjectHubService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hcr")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate a local project registry")
    validate.add_argument("registry", type=Path)
    validate.add_argument(
        "--allow-missing",
        action="store_true",
        help="schema/template check only; forbidden for daemon startup",
    )
    validate_hub = commands.add_parser("validate-hub", help="validate Project Hub config")
    validate_hub.add_argument("config", type=Path)
    validate_hub.add_argument("--allow-unbound", action="store_true")
    pilot = commands.add_parser("pilot", help="run one bound Codex/Telegram pilot turn")
    pilot.add_argument("config", type=Path)
    pilot.add_argument("--project", required=True)
    pilot.add_argument("--chat-id", required=True, type=int)
    pilot.add_argument("--thread-id", required=True, type=int)
    pilot.add_argument("--topic-title", required=True)
    serve = commands.add_parser("serve", help="run the managed Codex Telegram bot")
    serve.add_argument("config", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "validate-hub":
        try:
            config = load_hub_config(args.config, allow_unbound=args.allow_unbound)
            load_registry(config.registry_path)
        except (HubConfigError, RegistryError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 2
        print(
            json.dumps(
                {
                    "ok": True,
                    "projects": [project.project_id for project in config.projects],
                    "agents": [agent.agent_id for agent in config.agents],
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "serve":
        try:
            service = ProjectHubService(load_hub_config(args.config))
            service.run_forever()
        except KeyboardInterrupt:
            return 0
        except (HubConfigError, RegistryError, RuntimeError, OSError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 2
        finally:
            if "service" in locals():
                service.close()
        return 0
    if args.command == "pilot":
        try:
            config = load_hub_config(args.config)
            result = run_codex_pilot(
                config,
                project_id=args.project,
                chat_id=args.chat_id,
                thread_id=args.thread_id,
                topic_title=args.topic_title,
            )
        except (HubConfigError, RegistryError, RuntimeError, ValueError, OSError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 2
        print(
            json.dumps(
                {
                    "ok": True,
                    "local_session_id": result.local_session_id,
                    "provider_session_id": result.provider_session_id,
                    "telegram_message_id": result.telegram_message_id,
                    "terminal_name": result.terminal_name,
                },
                ensure_ascii=False,
            )
        )
        return 0
    try:
        registry = load_registry(args.registry, require_exists=not args.allow_missing)
    except RegistryError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "schema_version": registry.schema_version,
                "projects": [project.project_id for project in registry.projects],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
