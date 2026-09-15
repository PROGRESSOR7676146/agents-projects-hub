"""Explicit worker for user-authorized Telegram project-group provisioning."""

from __future__ import annotations

import asyncio
import fcntl
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from .hub_config import HubConfig
from .project_admin import ensure_project, prepare_project_root
from .project_onboarding import OnboardingWorkflow, ProjectOnboardingStore
from .registry import RegistryError, load_registry
from .runtime_health import PROJECT_PROVISIONER_INSTANCE_ID
from .state import HubState, StateError


class ProjectProvisioningError(RuntimeError):
    pass


class ProjectProvisionerStopping(RuntimeError):
    pass


class ProvisioningRejected(ProjectProvisioningError):
    pass


class ProvisioningUnknown(ProjectProvisioningError):
    pass


RPC_DEADLINE_SECONDS = 30.0
LEASE_HEARTBEAT_SECONDS = 20.0


def _session_path(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.suffix != ".session" or not expanded.is_absolute():
        raise ProjectProvisioningError("project provisioning session must be an absolute .session")
    return expanded.resolve(strict=False)


def _prepare_session_file(path: Path) -> None:
    parent = path.parent
    if not parent.is_dir() or parent.stat().st_mode & 0o077:
        raise ProjectProvisioningError("project provisioning session directory must be private")
    if path.exists():
        if not path.is_file() or path.stat().st_mode & 0o077:
            raise ProjectProvisioningError("project provisioning session must have mode 0600")
        return
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)


@contextmanager
def _private_umask():
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


class ProvisioningSessionLock:
    def __init__(self, session_path: Path) -> None:
        self.path = _session_path(session_path).with_suffix(".session.lock")
        self._descriptor: int | None = None

    def acquire(self) -> None:
        if not self.path.parent.is_dir() or self.path.parent.stat().st_mode & 0o077:
            raise ProjectProvisioningError("project provisioning session directory must be private")
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise ProjectProvisioningError(
                "project provisioning session is already in use"
            ) from None
        self._descriptor = descriptor

    def release(self) -> None:
        if self._descriptor is None:
            return
        fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        os.close(self._descriptor)
        self._descriptor = None


@dataclass(frozen=True, slots=True)
class CreatedForum:
    telegram_chat_id: int
    access_hash: int


class ProvisioningClient(Protocol):
    async def connect(self) -> None: ...

    async def identity(self) -> int: ...

    async def preflight_members(
        self,
        *,
        expected_creator_id: int,
        required_owner_ids: tuple[int, ...],
        hub_username: str,
        provider_usernames: tuple[str, ...],
        before_rpc: Callable[[], None],
    ) -> None: ...

    async def create_private_forum(self, title: str, about: str) -> CreatedForum: ...

    async def configure_group(
        self,
        group: CreatedForum,
        *,
        hub_username: str,
        provider_usernames: tuple[str, ...],
        before_rpc: Callable[[], None],
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
        self._session_path = _session_path(settings.session_path)
        _prepare_session_file(self._session_path)
        with _private_umask():
            self._client: Any = TelegramClient(
                str(self._session_path),
                settings.api_id,
                api_hash,
                request_retries=0,
                connection_retries=0,
                retry_delay=0,
                auto_reconnect=False,
                flood_sleep_threshold=0,
                raise_last_call_error=True,
                receive_updates=False,
            )
        self._owners: dict[int, Any] = {}
        self._bots: dict[str, Any] = {}

    @staticmethod
    async def _bounded(awaitable: Any) -> Any:
        return await asyncio.wait_for(awaitable, timeout=RPC_DEADLINE_SECONDS)

    async def connect(self) -> None:
        try:
            await self._bounded(self._client.connect())
            if not await self._bounded(self._client.is_user_authorized()):
                raise ProjectProvisioningError(
                    "Telegram provisioning user is not authorized; run project-provision-login"
                )
        except ProvisioningUnknown:
            await self._disconnect_after_unknown()
            raise
        except ProjectProvisioningError:
            raise
        except Exception as exc:
            classified = self._classify(exc)
            if isinstance(classified, ProvisioningUnknown):
                await self._disconnect_after_unknown()
            raise classified from exc

    async def identity(self) -> int:
        try:
            identity = await self._bounded(self._client.get_me())
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
            from telethon.errors import (
                BadRequestError,
                FloodError,
                ForbiddenError,
                InvalidDCError,
                UnauthorizedError,
            )
        except ImportError:
            known_rejections = ()
        else:
            known_rejections = (
                BadRequestError,
                FloodError,
                ForbiddenError,
                InvalidDCError,
                UnauthorizedError,
            )
        if isinstance(exc, known_rejections):
            return ProvisioningRejected(type(exc).__name__)
        if isinstance(exc, (OSError, TimeoutError, ConnectionError, asyncio.TimeoutError)):
            return ProvisioningUnknown(type(exc).__name__)
        return ProvisioningUnknown(type(exc).__name__)

    async def _disconnect_after_unknown(self) -> None:
        disconnect = getattr(self._client, "disconnect", None)
        if disconnect is None:
            return
        try:
            await asyncio.wait_for(disconnect(), timeout=5)
        except Exception:
            pass

    async def preflight_members(
        self,
        *,
        expected_creator_id: int,
        required_owner_ids: tuple[int, ...],
        hub_username: str,
        provider_usernames: tuple[str, ...],
        before_rpc: Callable[[], None],
    ) -> None:
        try:
            from telethon import types

            before_rpc()
            creator = await self._bounded(self._client.get_me())
            if int(getattr(creator, "id", 0)) != expected_creator_id:
                raise ProjectProvisioningError("project provisioning identity mismatch")
            owners: dict[int, Any] = {}
            for owner_id in required_owner_ids:
                if owner_id == expected_creator_id:
                    access_hash = getattr(creator, "access_hash", None)
                    if not isinstance(access_hash, int):
                        raise ProvisioningUnknown("owner_identity_invalid")
                    input_entity = types.InputUser(owner_id, access_hash)
                else:
                    before_rpc()
                    input_entity = await self._bounded(self._client.get_input_entity(owner_id))
                before_rpc()
                entity = await self._bounded(self._client.get_entity(input_entity))
                if (
                    int(getattr(entity, "id", 0)) != owner_id
                    or bool(getattr(entity, "bot", False))
                    or bool(getattr(entity, "deleted", False))
                ):
                    raise ProvisioningRejected("owner_identity_invalid")
                owners[owner_id] = input_entity
            bots: dict[str, Any] = {}
            for username in (hub_username, *provider_usernames):
                before_rpc()
                input_entity = await self._bounded(self._client.get_input_entity(username))
                before_rpc()
                entity = await self._bounded(self._client.get_entity(input_entity))
                if (
                    not bool(getattr(entity, "bot", False))
                    or str(getattr(entity, "username", "")).casefold()
                    != username.removeprefix("@").casefold()
                ):
                    raise ProvisioningRejected("bot_identity_invalid")
                bots[username.casefold()] = input_entity
            self._owners = owners
            self._bots = bots
        except ProjectProvisionerStopping:
            raise
        except ProvisioningUnknown:
            await self._disconnect_after_unknown()
            raise
        except ProjectProvisioningError:
            raise
        except Exception as exc:
            classified = self._classify(exc)
            if isinstance(classified, ProvisioningUnknown):
                await self._disconnect_after_unknown()
            raise classified from exc

    async def create_private_forum(self, title: str, about: str) -> CreatedForum:
        try:
            from telethon import functions, utils

            result = await self._bounded(
                self._client(
                    functions.channels.CreateChannelRequest(
                        title=title,
                        about=about,
                        megagroup=True,
                        forum=True,
                    )
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
        except ProvisioningUnknown:
            await self._disconnect_after_unknown()
            raise
        except ProjectProvisioningError:
            raise
        except Exception as exc:
            classified = self._classify(exc)
            if isinstance(classified, ProvisioningUnknown):
                await self._disconnect_after_unknown()
            raise classified from exc

    async def configure_group(
        self,
        group: CreatedForum,
        *,
        hub_username: str,
        provider_usernames: tuple[str, ...],
        before_rpc: Callable[[], None],
    ) -> None:
        try:
            from telethon import functions, types, utils
            from telethon.errors import UserAlreadyParticipantError, UserNotParticipantError

            channel_id, _ = utils.resolve_id(group.telegram_chat_id)
            channel = types.InputChannel(channel_id=channel_id, access_hash=group.access_hash)
            if not self._owners or not self._bots:
                raise ProjectProvisioningError("project provisioning preflight is missing")
            identities = {
                username.casefold(): self._bots[username.casefold()]
                for username in (hub_username, *provider_usernames)
            }

            def verified_active_participant(
                membership: object,
                identity: object,
                error_code: str,
            ) -> object:
                participant = getattr(membership, "participant", None)
                active_types = (
                    types.ChannelParticipant,
                    types.ChannelParticipantSelf,
                    types.ChannelParticipantCreator,
                    types.ChannelParticipantAdmin,
                )
                expected_user_id = getattr(identity, "user_id", None)
                if (
                    not isinstance(participant, active_types)
                    or expected_user_id is None
                    or getattr(participant, "user_id", None) != expected_user_id
                ):
                    raise ProvisioningUnknown(error_code)
                return participant

            before_rpc()
            creator_id = int((await self._bounded(self._client.get_me())).id)
            creator_identity = self._owners.get(creator_id)
            if creator_identity is None:
                raise ProjectProvisioningError("project provisioning creator preflight is missing")
            before_rpc()
            entity = await self._bounded(self._client.get_entity(channel))
            if (
                not getattr(entity, "forum", False)
                or not getattr(entity, "megagroup", False)
                or getattr(entity, "username", None) is not None
                or not getattr(entity, "creator", False)
            ):
                raise ProvisioningRejected("group_identity_invalid")
            before_rpc()
            creator_membership = await self._bounded(
                self._client(
                    functions.channels.GetParticipantRequest(
                        channel=channel,
                        participant=creator_identity,
                    )
                )
            )
            creator_participant = verified_active_participant(
                creator_membership,
                creator_identity,
                "group_creator_invalid",
            )
            if not isinstance(creator_participant, types.ChannelParticipantCreator):
                raise ProvisioningRejected("group_creator_invalid")
            owner_invitees = tuple(
                identity for owner_id, identity in self._owners.items() if owner_id != creator_id
            )
            for identity in owner_invitees:
                try:
                    before_rpc()
                    await self._bounded(
                        self._client(
                            functions.channels.InviteToChannelRequest(
                                channel=channel,
                                users=[identity],
                            )
                        )
                    )
                except UserAlreadyParticipantError:
                    pass
            owner_rights = types.ChatAdminRights(
                change_info=True,
                delete_messages=True,
                ban_users=True,
                invite_users=True,
                pin_messages=True,
                add_admins=True,
                manage_call=True,
                manage_topics=True,
                other=True,
            )
            for owner_id, identity in self._owners.items():
                if owner_id == creator_id:
                    continue
                before_rpc()
                await self._bounded(
                    self._client(
                        functions.channels.EditAdminRequest(
                            channel=channel,
                            user_id=identity,
                            admin_rights=owner_rights,
                            rank="Owner",
                        )
                    )
                )
            required_owner_rights = (
                "change_info",
                "delete_messages",
                "ban_users",
                "invite_users",
                "pin_messages",
                "add_admins",
                "manage_call",
                "manage_topics",
            )
            for owner_id, identity in self._owners.items():
                if owner_id == creator_id:
                    continue
                before_rpc()
                membership = await self._bounded(
                    self._client(
                        functions.channels.GetParticipantRequest(
                            channel=channel,
                            participant=identity,
                        )
                    )
                )
                participant = verified_active_participant(
                    membership,
                    identity,
                    "owner_admin_rights_unknown",
                )
                rights = getattr(participant, "admin_rights", None)
                if rights is None or not all(
                    getattr(rights, name, False) for name in required_owner_rights
                ):
                    raise ProvisioningUnknown("owner_admin_rights_unknown")
            for identity in identities.values():
                try:
                    before_rpc()
                    membership = await self._bounded(
                        self._client(
                            functions.channels.GetParticipantRequest(
                                channel=channel,
                                participant=identity,
                            )
                        )
                    )
                    verified_active_participant(
                        membership,
                        identity,
                        "bot_membership_unknown",
                    )
                except UserNotParticipantError:
                    try:
                        before_rpc()
                        await self._bounded(
                            self._client(
                                functions.channels.InviteToChannelRequest(
                                    channel=channel,
                                    users=[identity],
                                )
                            )
                        )
                    except UserAlreadyParticipantError:
                        pass
            before_rpc()
            await self._bounded(
                self._client(
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
            )
            before_rpc()
            entity = await self._bounded(self._client.get_entity(channel))
            if (
                not getattr(entity, "forum", False)
                or not getattr(entity, "megagroup", False)
                or getattr(entity, "username", None) is not None
                or not getattr(entity, "creator", False)
            ):
                raise ProvisioningUnknown("group_readiness_unknown")
            for username, identity in identities.items():
                before_rpc()
                membership = await self._bounded(
                    self._client(
                        functions.channels.GetParticipantRequest(
                            channel=channel,
                            participant=identity,
                        )
                    )
                )
                participant = verified_active_participant(
                    membership,
                    identity,
                    "bot_membership_unknown",
                )
                if username == hub_username.casefold():
                    rights = getattr(participant, "admin_rights", None)
                    if rights is None or not getattr(rights, "manage_topics", False):
                        raise ProvisioningUnknown("hub_manage_topics_unknown")
            for owner_id, identity in self._owners.items():
                before_rpc()
                membership = await self._bounded(
                    self._client(
                        functions.channels.GetParticipantRequest(
                            channel=channel,
                            participant=identity,
                        )
                    )
                )
                participant = verified_active_participant(
                    membership,
                    identity,
                    "owner_membership_unknown",
                )
                if owner_id == creator_id:
                    if not isinstance(participant, types.ChannelParticipantCreator):
                        raise ProvisioningUnknown("creator_membership_unknown")
                    continue
                rights = getattr(participant, "admin_rights", None)
                if rights is None or not all(
                    getattr(rights, name, False) for name in required_owner_rights
                ):
                    raise ProvisioningUnknown("owner_admin_rights_unknown")
        except ProjectProvisionerStopping:
            raise
        except ProvisioningUnknown:
            await self._disconnect_after_unknown()
            raise
        except ProjectProvisioningError:
            raise
        except Exception as exc:
            classified = self._classify(exc)
            if isinstance(classified, ProvisioningUnknown):
                await self._disconnect_after_unknown()
            raise classified from exc

    async def close(self) -> None:
        try:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        finally:
            for path in self._session_path.parent.glob(f"{self._session_path.name}*"):
                if path.is_file():
                    os.chmod(path, 0o600)


async def login_project_provisioner(config: HubConfig) -> dict[str, object]:
    settings = config.project_provisioning
    if settings.api_id is None or settings.api_hash_file is None or settings.session_path is None:
        raise ProjectProvisioningError("project provisioning credentials are incomplete")
    try:
        from telethon import TelegramClient
    except ImportError as exc:
        raise ProjectProvisioningError("install the project with the 'provisioning' extra") from exc
    session = _session_path(settings.session_path)
    lock = ProvisioningSessionLock(session)
    lock.acquire()
    client: Any = None
    mismatch = False
    try:
        api_hash = settings.api_hash_file.read_text(encoding="utf-8").strip()
        _prepare_session_file(session)
        with _private_umask():
            client = TelegramClient(
                str(session),
                settings.api_id,
                api_hash,
                request_retries=0,
                connection_retries=0,
                retry_delay=0,
                auto_reconnect=False,
                flood_sleep_threshold=0,
                raise_last_call_error=True,
                receive_updates=False,
            )
            await client.start()
            identity = await client.get_me()
            user_id = int(identity.id)
            if settings.expected_user_id is not None and user_id != settings.expected_user_id:
                mismatch = True
                raise ProjectProvisioningError(
                    "authorized Telegram account does not match project_provisioning.expected_user_id"
                )
            if user_id not in config.owner_user_ids:
                mismatch = True
                raise ProjectProvisioningError(
                    "provisioning Telegram account must be one of owner_user_ids"
                )
    finally:
        if client is not None:
            await client.disconnect()
        for path in session.parent.glob(f"{session.name}*"):
            if path.is_file():
                os.chmod(path, 0o600)
        if mismatch and session.exists():
            quarantine = session.with_name(
                f"{session.stem}.rejected-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.session"
            )
            os.replace(session, quarantine)
            os.chmod(quarantine, 0o600)
        lock.release()
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
        self._session_lock: ProvisioningSessionLock | None = None
        if client is None:
            settings = config.project_provisioning
            if settings.session_path is None:
                raise ProjectProvisioningError("project provisioning session is missing")
            self._session_lock = ProvisioningSessionLock(settings.session_path)
            self._session_lock.acquire()
        try:
            self.client = client or TelethonProvisioningClient(config)
        except Exception:
            if self._session_lock is not None:
                self._session_lock.release()
            self.state.close()
            raise
        self.worker_id = worker_id
        self._stop = threading.Event()
        self._lease_heartbeat_failed = threading.Event()
        self._started_at = datetime.now(timezone.utc)
        self._process_start_marker = uuid.uuid4().hex
        self._last_success_at: datetime | None = None
        self._last_error_code: str | None = None
        self._publish_health()

    def close(self) -> None:
        try:
            self.state.close()
        finally:
            if self._session_lock is not None:
                self._session_lock.release()

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

    async def _heartbeat(self, workflow_id: str, lease_token: str, finished: asyncio.Event) -> None:
        while not finished.is_set():
            try:
                await asyncio.wait_for(finished.wait(), timeout=LEASE_HEARTBEAT_SECONDS)
                return
            except asyncio.TimeoutError:
                try:
                    self.store.heartbeat_lease(workflow_id, lease_token)
                except StateError:
                    self._lease_heartbeat_failed.set()
                    return

    def _guard(self, workflow: OnboardingWorkflow, lease_token: str, *, expected: str) -> None:
        if self._stop.is_set() or self._lease_heartbeat_failed.is_set():
            try:
                self.store.release_before_external(
                    workflow.workflow_id, lease_token, expected=expected
                )
            except StateError:
                pass
            raise ProjectProvisionerStopping("onboarding worker is stopping")
        self.store.assert_lease(workflow.workflow_id, lease_token, expected=expected)

    def _block_external(
        self,
        workflow: OnboardingWorkflow,
        lease_token: str,
        *,
        resume_stage: str,
        error: BaseException,
    ) -> None:
        group_exists = workflow.telegram_chat_id is not None
        self.store.block(
            workflow.workflow_id,
            lease_token,
            expected=workflow.stage,
            resume_stage=resume_stage,
            error_code=(str(error) or type(error).__name__)[:128],
            notice=(
                "Настройка существующей группы остановлена до исправления доступа. "
                "Группа сохранена; после проверки выполните локальный resume этого workflow."
                if group_exists
                else "Создание группы остановлено до внешних изменений. После исправления "
                "доступа выполните локальный resume этого workflow."
            ),
        )
        self._publish_failure(type(error).__name__)

    async def _run_claimed(self, workflow: OnboardingWorkflow) -> bool:
        assert workflow.lease_token is not None
        lease = workflow.lease_token
        expected_owners = tuple(sorted(self.config.owner_user_ids))
        if workflow.required_owner_user_ids != expected_owners:
            self._block_external(
                workflow,
                lease,
                resume_stage=(
                    "configuring_group"
                    if workflow.telegram_chat_id is not None
                    else "preparing_root"
                ),
                error=ProjectProvisioningError("project owner snapshot changed"),
            )
            return True
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
                self._guard(workflow, lease, expected=workflow.stage)
                await self.client.connect()
                self._guard(workflow, lease, expected=workflow.stage)
                identity = await self.client.identity()
                expected = self.config.project_provisioning.expected_user_id
                if (
                    expected is None
                    or identity != expected
                    or identity not in self.config.owner_user_ids
                ):
                    raise ProjectProvisioningError("project provisioning identity mismatch")
                assert self.config.hub_bot is not None
                self._guard(workflow, lease, expected=workflow.stage)
                await self.client.preflight_members(
                    expected_creator_id=expected,
                    required_owner_ids=workflow.required_owner_user_ids,
                    hub_username=self.config.hub_bot.telegram_username,
                    provider_usernames=tuple(
                        agent.telegram_username
                        for agent in self.config.agents
                        if not agent.managed_externally
                    ),
                    before_rpc=lambda: self._guard(workflow, lease, expected=workflow.stage),
                )
            except (ProvisioningUnknown, ProvisioningRejected, ProjectProvisioningError) as exc:
                self._block_external(
                    workflow,
                    lease,
                    resume_stage=(
                        "configuring_group"
                        if workflow.telegram_chat_id is not None
                        else "preparing_root"
                    ),
                    error=exc,
                )
                return True
        if workflow.stage == "creating_group":
            try:
                self._guard(workflow, lease, expected="creating_group")
                self.store.assert_reservation(workflow.workflow_id)
                assert workflow.display_name is not None
                group = await self.client.create_private_forum(
                    workflow.display_name, self.config.project_provisioning.group_about
                )
            except ProvisioningUnknown as exc:
                self.store.mark_group_unknown(workflow.workflow_id, lease, str(exc))
                self._publish_failure(type(exc).__name__)
                return True
            except (ProvisioningRejected, ProjectProvisioningError) as exc:
                self._block_external(
                    workflow,
                    lease,
                    resume_stage="preparing_root",
                    error=exc,
                )
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
                self._guard(workflow, lease, expected="configuring_group")
                assert self.config.hub_bot is not None
                await self.client.configure_group(
                    group,
                    hub_username=self.config.hub_bot.telegram_username,
                    provider_usernames=tuple(
                        agent.telegram_username
                        for agent in self.config.agents
                        if not agent.managed_externally
                    ),
                    before_rpc=lambda: self._guard(workflow, lease, expected="configuring_group"),
                )
            except ProvisioningUnknown as exc:
                self.store.mark_configuration_unknown(workflow.workflow_id, lease, str(exc))
                self._publish_failure(type(exc).__name__)
                return True
            except (ProvisioningRejected, ProjectProvisioningError) as exc:
                self._block_external(
                    workflow,
                    lease,
                    resume_stage="configuring_group",
                    error=exc,
                )
                return True
            workflow = self.store.mark_configured(workflow.workflow_id, lease)
        if workflow.stage != "committing_binding":
            raise ProjectProvisioningError("onboarding worker reached an invalid stage")
        assert workflow.project_id is not None
        assert workflow.display_name is not None
        assert workflow.canonical_root is not None
        try:
            self.store.assert_lease(workflow.workflow_id, lease, expected="committing_binding")
            ensure_project(
                self.config.registry_path,
                project_id=workflow.project_id,
                display_name=workflow.display_name,
                root=workflow.canonical_root,
            )
            self.store.complete(workflow.workflow_id, lease)
            self._last_success_at = datetime.now(timezone.utc)
            self._last_error_code = None
        except (OSError, RegistryError) as exc:
            self.store.fail(
                workflow.workflow_id,
                lease,
                expected="committing_binding",
                error_code=type(exc).__name__,
            )
            self._last_error_code = type(exc).__name__
        self._publish_health()
        return True

    async def run_cycle_async(self) -> bool:
        if self._stop.is_set():
            self._publish_health()
            return False
        workflow = self.store.claim_next(self.worker_id)
        if workflow is None:
            self._publish_health()
            return False
        self._publish_health(workflow_id=workflow.workflow_id, activity_state="executing")
        assert workflow.lease_token is not None
        self._lease_heartbeat_failed.clear()
        finished = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(workflow.workflow_id, workflow.lease_token, finished)
        )
        try:
            try:
                return await self._run_claimed(workflow)
            except ProjectProvisionerStopping:
                self._publish_health()
                return False
        finally:
            finished.set()
            try:
                await heartbeat
            except (StateError, asyncio.CancelledError):
                pass

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
