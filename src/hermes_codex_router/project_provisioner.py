"""Explicit worker for user-authorized Telegram project-group provisioning."""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .hub_config import HubConfig
from .project_admin import ensure_project, prepare_project_root
from .project_onboarding import OnboardingWorkflow, ProjectOnboardingStore
from .registry import RegistryError, load_registry
from .runtime_health import PROJECT_PROVISIONER_INSTANCE_ID
from .state import HubState, StateError


class ProjectProvisioningError(RuntimeError):
    pass


class ProvisioningRejected(ProjectProvisioningError):
    pass


class ProvisioningUnknown(ProjectProvisioningError):
    pass


@dataclass(frozen=True, slots=True)
class CreatedForum:
    telegram_chat_id: int
    access_hash: int


class ProvisioningClient(Protocol):
    async def connect(self) -> None: ...

    async def identity(self) -> int: ...

    async def create_private_forum(self, title: str, about: str) -> CreatedForum: ...

    async def configure_bots(
        self,
        group: CreatedForum,
        *,
        hub_username: str,
        provider_usernames: tuple[str, ...],
    ) -> None: ...

    async def close(self) -> None: ...


class TelethonProvisioningClient:
    """Small MTProto adapter; it never receives project paths or bot tokens."""

    def __init__(self, config: HubConfig) -> None:
        settings = config.project_provisioning
        if (
            settings.api_id is None
            or settings.api_hash_file is None
            or settings.session_path is None
        ):
            raise ProjectProvisioningError("project provisioning credentials are incomplete")
        try:
            from telethon import TelegramClient
        except ImportError as exc:
            raise ProjectProvisioningError(
                "install the project with the 'provisioning' extra"
            ) from exc
        api_hash = settings.api_hash_file.read_text(encoding="utf-8").strip()
        self._session_path = settings.session_path
        self._client: Any = TelegramClient(str(settings.session_path), settings.api_id, api_hash)

    async def connect(self) -> None:
        try:
            await self._client.connect()
            if not await self._client.is_user_authorized():
                raise ProjectProvisioningError(
                    "Telegram provisioning user is not authorized; run project-provision-login"
                )
        except ProjectProvisioningError:
            raise
        except (OSError, TimeoutError, ConnectionError) as exc:
            raise ProvisioningUnknown(type(exc).__name__) from exc

    async def identity(self) -> int:
        try:
            identity = await self._client.get_me()
            user_id = getattr(identity, "id", None)
            if not isinstance(user_id, int) or user_id <= 0:
                raise ProjectProvisioningError("Telegram provisioning identity is invalid")
            return user_id
        except ProjectProvisioningError:
            raise
        except Exception as exc:
            raise self._classify(exc) from exc

    @staticmethod
    def _classify(exc: Exception) -> ProjectProvisioningError:
        try:
            from telethon.errors import RPCError
        except ImportError:
            RPCError = ()  # type: ignore[assignment,misc]
        if isinstance(exc, RPCError):
            return ProvisioningRejected(type(exc).__name__)
        if isinstance(exc, (OSError, TimeoutError, ConnectionError, asyncio.TimeoutError)):
            return ProvisioningUnknown(type(exc).__name__)
        return ProvisioningUnknown(type(exc).__name__)

    async def create_private_forum(self, title: str, about: str) -> CreatedForum:
        try:
            from telethon import functions, utils

            result = await self._client(
                functions.channels.CreateChannelRequest(
                    title=title,
                    about=about,
                    megagroup=True,
                    forum=True,
                )
            )
            chats = tuple(getattr(result, "chats", ()) or ())
            group = next(
                (
                    chat
                    for chat in chats
                    if getattr(chat, "megagroup", False) and getattr(chat, "forum", False)
                ),
                None,
            )
            access_hash = getattr(group, "access_hash", None)
            if group is None or not isinstance(access_hash, int):
                raise ProvisioningUnknown("create_channel_result_unknown")
            chat_id = int(utils.get_peer_id(group))
            if not str(chat_id).startswith("-100"):
                raise ProvisioningUnknown("create_channel_identity_unknown")
            return CreatedForum(chat_id, access_hash)
        except ProjectProvisioningError:
            raise
        except Exception as exc:
            raise self._classify(exc) from exc

    async def configure_bots(
        self,
        group: CreatedForum,
        *,
        hub_username: str,
        provider_usernames: tuple[str, ...],
    ) -> None:
        try:
            from telethon import functions, types, utils
            from telethon.errors import UserAlreadyParticipantError

            channel_id, _ = utils.resolve_id(group.telegram_chat_id)
            channel = types.InputChannel(channel_id=channel_id, access_hash=group.access_hash)
            identities: dict[str, Any] = {}
            for username in (hub_username, *provider_usernames):
                identity = await self._client.get_input_entity(username)
                identities[username.casefold()] = identity
                try:
                    await self._client(
                        functions.channels.InviteToChannelRequest(
                            channel=channel,
                            users=[identity],
                        )
                    )
                except UserAlreadyParticipantError:
                    pass
            await self._client(
                functions.channels.EditAdminRequest(
                    channel=channel,
                    user_id=identities[hub_username.casefold()],
                    admin_rights=types.ChatAdminRights(
                        invite_users=True,
                        manage_topics=True,
                        other=True,
                    ),
                    rank="Hub",
                )
            )
            entity = await self._client.get_entity(channel)
            if (
                not getattr(entity, "forum", False)
                or not getattr(entity, "megagroup", False)
                or getattr(entity, "username", None) is not None
            ):
                raise ProvisioningUnknown("group_readiness_unknown")
            for username, identity in identities.items():
                membership = await self._client(
                    functions.channels.GetParticipantRequest(
                        channel=channel,
                        participant=identity,
                    )
                )
                if username == hub_username.casefold():
                    participant = getattr(membership, "participant", None)
                    rights = getattr(participant, "admin_rights", None)
                    if rights is None or not getattr(rights, "manage_topics", False):
                        raise ProvisioningUnknown("hub_manage_topics_unknown")
        except ProjectProvisioningError:
            raise
        except Exception as exc:
            raise self._classify(exc) from exc

    async def close(self) -> None:
        try:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        finally:
            if self._session_path.exists():
                os.chmod(self._session_path, 0o600)


async def login_project_provisioner(config: HubConfig) -> dict[str, object]:
    settings = config.project_provisioning
    if settings.api_id is None or settings.api_hash_file is None or settings.session_path is None:
        raise ProjectProvisioningError("project provisioning credentials are incomplete")
    try:
        from telethon import TelegramClient
    except ImportError as exc:
        raise ProjectProvisioningError("install the project with the 'provisioning' extra") from exc
    api_hash = settings.api_hash_file.read_text(encoding="utf-8").strip()
    client: Any = TelegramClient(str(settings.session_path), settings.api_id, api_hash)
    try:
        await client.start()
        identity = await client.get_me()
        user_id = int(identity.id)
        if settings.expected_user_id is not None and user_id != settings.expected_user_id:
            raise ProjectProvisioningError(
                "authorized Telegram account does not match project_provisioning.expected_user_id"
            )
        if user_id not in config.owner_user_ids:
            raise ProjectProvisioningError(
                "provisioning Telegram account must be one of owner_user_ids"
            )
    finally:
        await client.disconnect()
    os.chmod(settings.session_path, 0o600)
    return {"ok": True, "authorized": True, "user_id": user_id}


class ProjectProvisioner:
    def __init__(
        self,
        config: HubConfig,
        *,
        client: ProvisioningClient | None = None,
        worker_id: str = "project-provisioner",
    ) -> None:
        if not config.project_provisioning.enabled:
            raise ProjectProvisioningError("project provisioning is disabled")
        self.config = config
        self.registry = load_registry(config.registry_path)
        self.state = HubState.open(config.state_path)
        self.store = ProjectOnboardingStore(self.state)
        self.client = client or TelethonProvisioningClient(config)
        self.worker_id = worker_id
        self._started_at = datetime.now(timezone.utc)
        self._process_start_marker = uuid.uuid4().hex
        self._last_success_at: datetime | None = None
        self._last_error_code: str | None = None
        self._publish_health()

    def close(self) -> None:
        self.state.close()

    def _prepare_root(self, workflow: OnboardingWorkflow) -> Path:
        if workflow.base_root is None or workflow.project_id is None:
            raise RegistryError("onboarding workflow has no project root")
        self.registry = load_registry(self.config.registry_path)
        if workflow.base_root not in self.registry.allowed_roots:
            raise RegistryError("onboarding base root is no longer allowed")
        if any(
            item.project_id == workflow.project_id and item.telegram_chat_id is not None
            for item in self.config.projects
        ):
            raise RegistryError("project already has a configured Telegram group")
        conflicts = [
            item
            for item in self.registry.projects
            if item.project_id == workflow.project_id or item.root == workflow.canonical_root
        ]
        if conflicts and not (
            len(conflicts) == 1
            and conflicts[0].project_id == workflow.project_id
            and conflicts[0].root == workflow.canonical_root
            and conflicts[0].enabled
        ):
            raise RegistryError("project registry identity conflicts with onboarding workflow")
        root = prepare_project_root(workflow.base_root, workflow.project_id)
        if workflow.canonical_root is None or root != workflow.canonical_root:
            raise RegistryError("prepared root does not match onboarding workflow")
        return root

    def _publish_health(
        self, *, workflow_id: str | None = None, activity_state: str = "idle"
    ) -> None:
        try:
            self.state.upsert_runtime_health(
                component="project_provisioner",
                instance_id=PROJECT_PROVISIONER_INSTANCE_ID,
                runtime="telegram-user",
                agent_id=None,
                pid=os.getpid(),
                process_start_marker=self._process_start_marker,
                started_at=self._started_at,
                heartbeat_at=datetime.now(timezone.utc),
                success_at=self._last_success_at,
                error_code=self._last_error_code,
                activity_state=activity_state,
                active_job_id=workflow_id,
            )
        except Exception:
            pass

    def _publish_failure(self, error_code: str) -> None:
        self._last_error_code = error_code[:128]
        self._publish_health()

    async def run_cycle_async(self) -> bool:
        workflow = self.store.claim_next(self.worker_id)
        if workflow is None:
            self._publish_health()
            return False
        self._publish_health(workflow_id=workflow.workflow_id, activity_state="executing")
        assert workflow.lease_token is not None
        lease = workflow.lease_token
        if workflow.stage == "preparing_root":
            try:
                self._prepare_root(workflow)
            except (OSError, RegistryError) as exc:
                self.store.fail(
                    workflow.workflow_id,
                    lease,
                    expected="preparing_root",
                    error_code=type(exc).__name__,
                )
                self._publish_failure(type(exc).__name__)
                return True
            workflow = self.store.mark_root_ready(workflow.workflow_id, lease)
        if workflow.stage in {"creating_group", "configuring_group"}:
            try:
                await self.client.connect()
                identity = await self.client.identity()
                expected = self.config.project_provisioning.expected_user_id
                if (
                    expected is None
                    or identity != expected
                    or identity not in self.config.owner_user_ids
                ):
                    raise ProjectProvisioningError("project provisioning identity mismatch")
            except ProvisioningUnknown as exc:
                if workflow.stage == "creating_group":
                    self.store.mark_group_unknown(workflow.workflow_id, lease, str(exc))
                else:
                    self.store.mark_configuration_unknown(workflow.workflow_id, lease, str(exc))
                self._publish_failure(type(exc).__name__)
                return True
            except (ProvisioningRejected, ProjectProvisioningError) as exc:
                self.store.fail(
                    workflow.workflow_id,
                    lease,
                    expected=workflow.stage,
                    error_code=type(exc).__name__,
                )
                self._publish_failure(type(exc).__name__)
                return True
        if workflow.stage == "creating_group":
            try:
                assert workflow.display_name is not None
                group = await self.client.create_private_forum(
                    workflow.display_name, self.config.project_provisioning.group_about
                )
            except ProvisioningUnknown as exc:
                self.store.mark_group_unknown(workflow.workflow_id, lease, str(exc))
                self._publish_failure(type(exc).__name__)
                return True
            except (ProvisioningRejected, ProjectProvisioningError) as exc:
                self.store.fail(
                    workflow.workflow_id,
                    lease,
                    expected="creating_group",
                    error_code=type(exc).__name__,
                )
                self._publish_failure(type(exc).__name__)
                return True
            workflow = self.store.mark_group_created(
                workflow.workflow_id,
                lease,
                telegram_chat_id=group.telegram_chat_id,
                telegram_access_hash=group.access_hash,
            )
        if workflow.stage == "configuring_group":
            assert workflow.telegram_chat_id is not None
            assert workflow.telegram_access_hash is not None
            group = CreatedForum(workflow.telegram_chat_id, workflow.telegram_access_hash)
            try:
                assert self.config.hub_bot is not None
                await self.client.configure_bots(
                    group,
                    hub_username=self.config.hub_bot.telegram_username,
                    provider_usernames=tuple(
                        agent.telegram_username for agent in self.config.agents
                    ),
                )
            except ProvisioningUnknown as exc:
                self.store.mark_configuration_unknown(workflow.workflow_id, lease, str(exc))
                self._publish_failure(type(exc).__name__)
                return True
            except (ProvisioningRejected, ProjectProvisioningError) as exc:
                self.store.fail(
                    workflow.workflow_id,
                    lease,
                    expected="configuring_group",
                    error_code=type(exc).__name__,
                )
                self._publish_failure(type(exc).__name__)
                return True
            workflow = self.store.mark_configured(workflow.workflow_id, lease)
        if workflow.stage != "committing_binding":
            raise ProjectProvisioningError("onboarding worker reached an invalid stage")
        assert workflow.project_id is not None
        assert workflow.display_name is not None
        assert workflow.canonical_root is not None
        try:
            ensure_project(
                self.config.registry_path,
                project_id=workflow.project_id,
                display_name=workflow.display_name,
                root=workflow.canonical_root,
            )
            self.store.complete(workflow.workflow_id, lease)
            self._last_success_at = datetime.now(timezone.utc)
            self._last_error_code = None
        except (OSError, RegistryError, StateError) as exc:
            self.store.fail(
                workflow.workflow_id,
                lease,
                expected="committing_binding",
                error_code=type(exc).__name__,
            )
            self._last_error_code = type(exc).__name__
        self._publish_health()
        return True

    def run_cycle(self) -> bool:
        async def once() -> bool:
            try:
                return await self.run_cycle_async()
            finally:
                await self.client.close()

        return asyncio.run(once())

    def request_stop(self) -> None:
        stop = getattr(self, "_stop", None)
        if stop is None:
            stop = self._stop = threading.Event()
        stop.set()

    def stop(self) -> None:
        self.request_stop()

    def run_forever(self, *, poll_seconds: float = 2.0) -> None:
        if not 0.1 <= poll_seconds <= 60:
            raise ValueError("poll_seconds must be between 0.1 and 60")
        stop = getattr(self, "_stop", None)
        if stop is None:
            stop = self._stop = threading.Event()

        async def loop() -> None:
            try:
                while not stop.is_set():
                    worked = await self.run_cycle_async()
                    if not worked:
                        await asyncio.sleep(poll_seconds)
            finally:
                await self.client.close()

        asyncio.run(loop())
