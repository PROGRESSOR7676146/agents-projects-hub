from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .codex_worker import CodexQueueWorker
from .command_menu import configure_public_commands
from .diagnostics import run_doctor
from .external_service import ExternalAgentService
from .hub_config import HubConfigError, load_codex_worker_config, load_hub_config
from .migrations import backup_database, migrate_database
from .monitoring import run_monitor_once
from .pilot import run_codex_pilot
from .project_admin import add_project, set_project_enabled
from .registry import RegistryError, load_registry
from .service import ProjectHubService
from .state import HubState, StateError
from .worktrees import WorktreeError, cleanup_worktree, create_worktree


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agents-projects-hub")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate a local project registry")
    validate.add_argument("registry", type=Path)
    validate.add_argument("--allow-missing", action="store_true")

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
    serve.add_argument("--agent", default="codex")

    worker = commands.add_parser("worker", help="run the isolated Codex queue worker")
    worker.add_argument("config", type=Path)
    worker.add_argument("--poll-seconds", type=float, default=0.2)

    doctor = commands.add_parser("doctor", help="run local deployment diagnostics")
    doctor.add_argument("config", type=Path)

    status = commands.add_parser("status", help="print persisted topic/session status")
    status.add_argument("config", type=Path)

    migrate = commands.add_parser("migrate", help="migrate a state database safely")
    migrate.add_argument("state", type=Path)
    migrate.add_argument("--no-backup", action="store_true")

    backup = commands.add_parser("backup", help="create an SQLite-consistent state backup")
    backup.add_argument("state", type=Path)
    backup.add_argument("destination", nargs="?", type=Path)

    monitor = commands.add_parser("monitor", help="evaluate operational alerts once")
    monitor.add_argument("config", type=Path)
    monitor.add_argument("--notify", action="store_true")
    monitor.add_argument("--repair", action="store_true")
    monitor.add_argument("--cooldown-seconds", type=int, default=3600)

    telegram_commands = commands.add_parser(
        "telegram-commands", help="check or synchronize the public Telegram command menu"
    )
    telegram_commands.add_argument("config", type=Path)
    telegram_commands.add_argument("--sync", action="store_true")

    project = commands.add_parser("project", help="manage the local project registry")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_list = project_commands.add_parser("list")
    project_list.add_argument("registry", type=Path)
    project_add = project_commands.add_parser("add")
    project_add.add_argument("registry", type=Path)
    project_add.add_argument("--id", required=True)
    project_add.add_argument("--name", required=True)
    project_add.add_argument("--topic", required=True)
    project_add.add_argument("--root", required=True, type=Path)
    for name in ("enable", "disable"):
        item = project_commands.add_parser(name)
        item.add_argument("registry", type=Path)
        item.add_argument("project_id")

    lane = commands.add_parser("lane", help="manage worktree-backed parallel lanes")
    lane_commands = lane.add_subparsers(dest="lane_command", required=True)
    lane_list = lane_commands.add_parser("list")
    lane_list.add_argument("config", type=Path)
    lane_create = lane_commands.add_parser("create")
    lane_create.add_argument("config", type=Path)
    lane_create.add_argument("--project", required=True)
    lane_create.add_argument("--lane", required=True)
    lane_create.add_argument("--branch")
    lane_archive = lane_commands.add_parser("archive")
    lane_archive.add_argument("config", type=Path)
    lane_archive.add_argument("--lane", required=True)
    lane_bind = lane_commands.add_parser("bind")
    lane_bind.add_argument("config", type=Path)
    lane_bind.add_argument("--lane", required=True)
    lane_bind.add_argument("--chat-id", required=True, type=int)
    lane_bind.add_argument("--thread-id", required=True, type=int)
    lane_bind.add_argument("--confirm", required=True)
    lane_cleanup = lane_commands.add_parser("cleanup")
    lane_cleanup.add_argument("config", type=Path)
    lane_cleanup.add_argument("--lane", required=True)
    lane_cleanup.add_argument("--confirm", required=True)
    return parser


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _project_command(args: argparse.Namespace) -> int:
    if args.project_command == "list":
        registry = load_registry(args.registry)
        _print(
            {
                "ok": True,
                "projects": [
                    {
                        "project_id": item.project_id,
                        "display_name": item.display_name,
                        "root": str(item.root),
                        "enabled": item.enabled,
                    }
                    for item in registry.projects
                ],
            }
        )
        return 0
    if args.project_command == "add":
        add_project(
            args.registry,
            project_id=args.id,
            display_name=args.name,
            topic_name=args.topic,
            root=args.root,
        )
        _print({"ok": True, "project_id": args.id})
        return 0
    enabled = args.project_command == "enable"
    set_project_enabled(args.registry, args.project_id, enabled)
    _print({"ok": True, "project_id": args.project_id, "enabled": enabled})
    return 0


def _lane_command(args: argparse.Namespace) -> int:
    config = load_hub_config(args.config)
    state = HubState.open(config.state_path)
    try:
        if args.lane_command == "list":
            _print({"ok": True, "lanes": state.list_lanes()})
            return 0
        if args.lane_command == "archive":
            state.archive_lane(args.lane)
            _print({"ok": True, "lane_id": args.lane, "status": "archived"})
            return 0
        if args.lane_command == "bind":
            expected = f"{args.chat_id}:{args.thread_id}"
            if args.confirm != expected:
                raise WorktreeError(f"binding confirmation must equal {expected}")
            topic = state.find_topic(args.chat_id, args.thread_id)
            if topic is None:
                raise WorktreeError("Telegram topic must be observed before lane binding")
            lane = state.bind_lane(args.lane, topic.topic_id)
            _print(
                {
                    "ok": True,
                    "lane_id": args.lane,
                    "chat_id": args.chat_id,
                    "thread_id": args.thread_id,
                    "topic_id": lane["topic_id"],
                }
            )
            return 0
        if args.lane_command == "cleanup":
            if args.confirm != args.lane:
                raise WorktreeError("cleanup confirmation must exactly equal the lane id")
            lane = state.get_lane(args.lane)
            if lane["status"] != "archived":
                raise WorktreeError("archive the lane before cleanup")
            if lane["cleaned_at"] is not None:
                raise WorktreeError("lane worktree was already cleaned")
            registry = load_registry(config.registry_path)
            project = registry.require_project(str(lane["project_id"]))
            cleanup_worktree(
                project,
                args.lane,
                recorded_path=Path(str(lane["worktree_path"])),
            )
            state.mark_lane_cleaned(args.lane)
            _print(
                {
                    "ok": True,
                    "lane_id": args.lane,
                    "status": "cleaned",
                    "branch_retained": lane["branch_name"],
                }
            )
            return 0
        registry = load_registry(config.registry_path)
        project = registry.require_project(args.project)
        path, branch = create_worktree(project, args.lane, branch_name=args.branch)
        state.register_lane(
            lane_id=args.lane,
            project_id=project.project_id,
            worktree_path=path,
            branch_name=branch,
        )
        _print(
            {
                "ok": True,
                "lane_id": args.lane,
                "project_id": project.project_id,
                "path": str(path),
                "branch": branch,
            }
        )
        return 0
    finally:
        state.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-hub":
            config = load_hub_config(args.config, allow_unbound=args.allow_unbound)
            load_registry(config.registry_path)
            _print(
                {
                    "ok": True,
                    "projects": [project.project_id for project in config.projects],
                    "agents": [agent.agent_id for agent in config.agents],
                }
            )
            return 0
        if args.command == "serve":
            config = load_hub_config(args.config)
            service = (
                ProjectHubService(config)
                if args.agent == "codex"
                else ExternalAgentService(config, args.agent, direct_messages_only=True)
            )
            try:
                service.run_forever()
            finally:
                service.close()
            return 0
        if args.command == "worker":
            worker = CodexQueueWorker(load_codex_worker_config(args.config))
            try:
                worker.run_forever(poll_seconds=args.poll_seconds)
            finally:
                worker.close()
            return 0
        if args.command == "pilot":
            result = run_codex_pilot(
                load_hub_config(args.config),
                project_id=args.project,
                chat_id=args.chat_id,
                thread_id=args.thread_id,
                topic_title=args.topic_title,
            )
            _print(
                {
                    "ok": True,
                    "local_session_id": result.local_session_id,
                    "provider_session_id": result.provider_session_id,
                    "telegram_message_id": result.telegram_message_id,
                    "terminal_name": result.terminal_name,
                }
            )
            return 0
        if args.command == "doctor":
            result = run_doctor(load_hub_config(args.config))
            _print(result)
            return 0 if result["ok"] else 1
        if args.command == "status":
            config = load_hub_config(args.config)
            state = HubState.open(config.state_path)
            try:
                result = {"ok": True, **state.status_snapshot()}
                if config.codex_multi_auth_dir is not None:
                    from .codex_accounts import read_codex_pool_status

                    result["codex_account_pool"] = read_codex_pool_status(
                        config.codex_multi_auth_dir,
                        executable=str(config.codex_multi_auth_executable)
                        if config.codex_multi_auth_executable
                        else "codex-multi-auth",
                    ).as_dict()
                _print(result)
            finally:
                state.close()
            return 0
        if args.command == "migrate":
            result = migrate_database(args.state, create_backup=not args.no_backup)
            _print(
                {
                    "ok": True,
                    "previous_version": result.previous_version,
                    "current_version": result.current_version,
                    "backup_path": str(result.backup_path) if result.backup_path else None,
                }
            )
            return 0
        if args.command == "backup":
            destination = backup_database(args.state, args.destination)
            _print({"ok": True, "backup_path": str(destination)})
            return 0
        if args.command == "monitor":
            if args.cooldown_seconds < 0:
                raise ValueError("cooldown-seconds cannot be negative")
            result = run_monitor_once(
                load_hub_config(args.config),
                notify=args.notify,
                repair=args.repair,
                cooldown_seconds=args.cooldown_seconds,
            )
            _print(result)
            return 0
        if args.command == "telegram-commands":
            result = configure_public_commands(load_hub_config(args.config), sync=args.sync)
            _print(result)
            return 0 if result["ok"] else 1
        if args.command == "project":
            return _project_command(args)
        if args.command == "lane":
            return _lane_command(args)

        registry = load_registry(args.registry, require_exists=not args.allow_missing)
        _print(
            {
                "ok": True,
                "schema_version": registry.schema_version,
                "projects": [project.project_id for project in registry.projects],
            }
        )
        return 0
    except KeyboardInterrupt:
        return 130
    except (
        HubConfigError,
        RegistryError,
        StateError,
        WorktreeError,
        RuntimeError,
        ValueError,
        OSError,
    ) as exc:
        _print({"ok": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
