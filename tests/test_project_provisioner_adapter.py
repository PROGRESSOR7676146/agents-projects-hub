from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from telethon import types
from telethon.errors import BadRequestError, ServerError, UserNotParticipantError

from hermes_codex_router.hub_config import (
    HubConfig,
    ProjectProvisioningSettings,
    TerminalSettings,
)
from hermes_codex_router.project_provisioner import (
    CreatedForum,
    ProjectProvisionerStopping,
    ProjectProvisioningError,
    ProvisioningRejected,
    ProvisioningSessionLock,
    ProvisioningUnknown,
    TelethonProvisioningClient,
    _prepare_session_file,
    _session_path,
    login_project_provisioner,
)


class ProjectProvisionerAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.session = self.base / "owner.session"
        self.api_hash = self.base / "api-hash"
        self.api_hash.write_text("0" * 32)
        os.chmod(self.api_hash, 0o600)
        self.config = HubConfig(
            schema_version=1,
            owner_user_ids=(42, 43),
            registry_path=self.base / "registry.json",
            state_path=self.base / "state.db",
            codex_socket_path=self.base / "codex.sock",
            manage_codex_server=False,
            terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
            projects=(),
            agents=(),
            project_provisioning=ProjectProvisioningSettings(
                True, 12345, self.api_hash, self.session, 42, "Example"
            ),
        )

    def test_session_is_precreated_private_and_lock_is_exclusive(self) -> None:
        previous = os.umask(0o022)
        try:
            _prepare_session_file(self.session)
        finally:
            os.umask(previous)
        self.assertEqual(self.session.stat().st_mode & 0o777, 0o600)
        first = ProvisioningSessionLock(self.session)
        second = ProvisioningSessionLock(self.session)
        first.acquire()
        try:
            with self.assertRaisesRegex(ProjectProvisioningError, "already in use"):
                second.acquire()
        finally:
            first.release()
        second.acquire()
        second.release()
        with self.assertRaisesRegex(ProjectProvisioningError, "absolute .session"):
            _session_path(self.base / "owner")

    def test_client_disables_telethon_retry_and_reconnect(self) -> None:
        client = TelethonProvisioningClient(self.config)
        try:
            self.assertEqual(client._client._request_retries, 0)
            self.assertEqual(client._client._connection_retries, 0)
            self.assertFalse(client._client._auto_reconnect)
            self.assertEqual(client._client.flood_sleep_threshold, 0)
            self.assertTrue(client._client._raise_last_call_error)
        finally:
            asyncio.run(client.close())

    def test_server_and_timeout_are_unknown_but_bad_request_is_rejected(self) -> None:
        self.assertIsInstance(
            TelethonProvisioningClient._classify(ServerError(None, "server")),
            ProvisioningUnknown,
        )
        self.assertIsInstance(
            TelethonProvisioningClient._classify(BadRequestError(None, "bad")),
            ProvisioningRejected,
        )
        self.assertIsInstance(
            TelethonProvisioningClient._classify(asyncio.TimeoutError()),
            ProvisioningUnknown,
        )

    def test_preflight_rejects_wrong_owner_and_bot_identities(self) -> None:
        class Client:
            async def get_me(self) -> object:
                return SimpleNamespace(id=42, bot=False, deleted=False)

            async def get_input_entity(self, reference: object) -> object:
                return reference

            async def get_entity(self, reference: object) -> object:
                if getattr(reference, "id", None) == 42:
                    return SimpleNamespace(id=42, bot=False, deleted=False)
                if reference == 43:
                    return SimpleNamespace(id=43, bot=True, deleted=False)
                return SimpleNamespace(id=100, bot=True, deleted=False, username="wrong_bot")

        client = TelethonProvisioningClient.__new__(TelethonProvisioningClient)
        client._client = Client()
        client._owners = {}
        client._bots = {}
        with self.assertRaises(ProvisioningRejected):
            asyncio.run(
                client.preflight_members(
                    expected_creator_id=42,
                    required_owner_ids=(42, 43),
                    hub_username="hub_bot",
                    provider_usernames=(),
                    before_rpc=lambda: None,
                )
            )

        with self.assertRaises(ProvisioningRejected):
            asyncio.run(
                client.preflight_members(
                    expected_creator_id=42,
                    required_owner_ids=(42,),
                    hub_username="hub_bot",
                    provider_usernames=(),
                    before_rpc=lambda: None,
                )
            )

    def test_create_timeout_sends_exactly_one_request(self) -> None:
        class HangingClient:
            def __init__(self) -> None:
                self.calls = 0

            async def __call__(self, _request: object) -> object:
                self.calls += 1
                await asyncio.Event().wait()
                raise AssertionError

        transport = HangingClient()
        client = TelethonProvisioningClient.__new__(TelethonProvisioningClient)
        client._client = transport
        with patch("hermes_codex_router.project_provisioner.RPC_DEADLINE_SECONDS", 0.01):
            with self.assertRaises(ProvisioningUnknown):
                asyncio.run(client.create_private_forum("Example", "Example"))
        self.assertEqual(transport.calls, 1)

    def test_group_configuration_invites_and_promotes_second_owner(self) -> None:
        creator = types.InputUser(42, 420)
        second_owner = types.InputUser(43, 430)
        hub = types.InputUser(100, 1000)
        provider = types.InputUser(101, 1010)

        class Client:
            def __init__(self) -> None:
                self.requests: list[object] = []
                self.invited: set[int] = set()

            async def get_me(self) -> object:
                return SimpleNamespace(id=42)

            async def get_entity(self, _entity: object) -> object:
                return SimpleNamespace(forum=True, megagroup=True, username=None, creator=True)

            async def __call__(self, request: object) -> object:
                self.requests.append(request)
                if type(request).__name__ == "GetParticipantRequest":
                    participant = cast(Any, request).participant
                    if participant == creator:
                        return SimpleNamespace(
                            participant=types.ChannelParticipantCreator(
                                user_id=creator.user_id,
                                admin_rights=types.ChatAdminRights(other=True),
                            )
                        )
                    if participant == second_owner:
                        rights = types.ChatAdminRights(
                            change_info=True,
                            delete_messages=True,
                            ban_users=True,
                            invite_users=True,
                            pin_messages=True,
                            add_admins=True,
                            manage_call=True,
                            manage_topics=True,
                        )
                        return SimpleNamespace(
                            participant=types.ChannelParticipantAdmin(
                                user_id=second_owner.user_id,
                                promoted_by=creator.user_id,
                                date=None,
                                admin_rights=rights,
                            )
                        )
                    if participant == hub:
                        if hub.user_id not in self.invited:
                            raise UserNotParticipantError(request)
                        return SimpleNamespace(
                            participant=types.ChannelParticipantAdmin(
                                user_id=hub.user_id,
                                promoted_by=creator.user_id,
                                date=None,
                                admin_rights=types.ChatAdminRights(manage_topics=True),
                            )
                        )
                    if participant == provider and provider.user_id not in self.invited:
                        raise UserNotParticipantError(request)
                    return SimpleNamespace(
                        participant=types.ChannelParticipant(user_id=provider.user_id, date=None)
                    )
                if type(request).__name__ == "InviteToChannelRequest":
                    self.invited.update(
                        cast(Any, identity).user_id for identity in cast(Any, request).users
                    )
                return SimpleNamespace()

        transport = Client()
        client = TelethonProvisioningClient.__new__(TelethonProvisioningClient)
        client._client = transport
        client._owners = {42: creator, 43: second_owner}
        client._bots = {"hub_bot": hub, "provider_bot": provider}
        asyncio.run(
            client.configure_group(
                CreatedForum(-1001234567890, 99),
                hub_username="hub_bot",
                provider_usernames=("provider_bot",),
                before_rpc=lambda: None,
            )
        )
        invites = [
            item for item in transport.requests if type(item).__name__ == "InviteToChannelRequest"
        ]
        promotions = [
            item for item in transport.requests if type(item).__name__ == "EditAdminRequest"
        ]
        self.assertEqual(len(invites), 3)
        self.assertEqual(
            {cast(Any, item).user_id.user_id for item in promotions},
            {second_owner.user_id, hub.user_id},
        )
        mutations = [
            item
            for item in transport.requests
            if type(item).__name__ in {"InviteToChannelRequest", "EditAdminRequest"}
        ]
        self.assertEqual(
            [
                (
                    type(item).__name__,
                    cast(Any, item).users[0].user_id
                    if type(item).__name__ == "InviteToChannelRequest"
                    else cast(Any, item).user_id.user_id,
                )
                for item in mutations
            ],
            [
                ("InviteToChannelRequest", second_owner.user_id),
                ("EditAdminRequest", second_owner.user_id),
                ("InviteToChannelRequest", hub.user_id),
                ("InviteToChannelRequest", provider.user_id),
                ("EditAdminRequest", hub.user_id),
            ],
        )
        request_names_and_participants = [
            (
                type(item).__name__,
                getattr(getattr(item, "participant", None), "user_id", None),
            )
            for item in transport.requests
        ]
        owner_readback = request_names_and_participants.index(
            ("GetParticipantRequest", second_owner.user_id), 3
        )
        first_bot_invite = next(
            index
            for index, item in enumerate(transport.requests)
            if type(item).__name__ == "InviteToChannelRequest"
            and cast(Any, item).users[0].user_id == hub.user_id
        )
        self.assertLess(owner_readback, first_bot_invite)

    def test_existing_bot_member_skips_rejected_reinvite_on_resume(self) -> None:
        creator = types.InputUser(42, 420)
        hub = types.InputUser(100, 1000)
        provider = types.InputUser(101, 1010)

        class Client:
            def __init__(self) -> None:
                self.requests: list[object] = []

            async def get_me(self) -> object:
                return SimpleNamespace(id=42)

            async def get_entity(self, _entity: object) -> object:
                return SimpleNamespace(forum=True, megagroup=True, username=None, creator=True)

            async def __call__(self, request: object) -> object:
                self.requests.append(request)
                if type(request).__name__ == "InviteToChannelRequest":
                    raise BadRequestError(request, "reinvite rejected")
                if type(request).__name__ == "GetParticipantRequest":
                    participant = cast(Any, request).participant
                    if participant == creator:
                        return SimpleNamespace(
                            participant=types.ChannelParticipantCreator(
                                user_id=creator.user_id,
                                admin_rights=types.ChatAdminRights(other=True),
                            )
                        )
                    if participant == hub:
                        return SimpleNamespace(
                            participant=types.ChannelParticipantAdmin(
                                user_id=hub.user_id,
                                promoted_by=creator.user_id,
                                date=None,
                                admin_rights=types.ChatAdminRights(manage_topics=True),
                            )
                        )
                    return SimpleNamespace(
                        participant=types.ChannelParticipant(user_id=provider.user_id, date=None)
                    )
                return SimpleNamespace()

        transport = Client()
        client = TelethonProvisioningClient.__new__(TelethonProvisioningClient)
        client._client = transport
        client._owners = {42: creator}
        client._bots = {"hub_bot": hub, "provider_bot": provider}
        asyncio.run(
            client.configure_group(
                CreatedForum(-1001234567890, 99),
                hub_username="hub_bot",
                provider_usernames=("provider_bot",),
                before_rpc=lambda: None,
            )
        )
        self.assertFalse(
            any(type(item).__name__ == "InviteToChannelRequest" for item in transport.requests)
        )

    def test_non_active_or_mismatched_participant_does_not_permit_invite(self) -> None:
        creator = types.InputUser(42, 420)
        hub = types.InputUser(100, 1000)
        invalid_participants = (
            types.ChannelParticipantLeft(peer=types.PeerUser(hub.user_id)),
            types.ChannelParticipantBanned(
                peer=types.PeerUser(hub.user_id),
                kicked_by=creator.user_id,
                date=None,
                banned_rights=types.ChatBannedRights(until_date=None, view_messages=True),
                left=True,
            ),
            types.ChannelParticipant(user_id=999, date=None),
            SimpleNamespace(user_id=hub.user_id),
        )

        for invalid_participant in invalid_participants:
            with self.subTest(participant=type(invalid_participant).__name__):

                class Client:
                    def __init__(self) -> None:
                        self.requests: list[object] = []

                    async def get_me(self) -> object:
                        return SimpleNamespace(id=creator.user_id)

                    async def get_entity(self, _entity: object) -> object:
                        return SimpleNamespace(
                            forum=True,
                            megagroup=True,
                            username=None,
                            creator=True,
                        )

                    async def __call__(self, request: object) -> object:
                        self.requests.append(request)
                        if type(request).__name__ == "GetParticipantRequest":
                            if cast(Any, request).participant == creator:
                                return SimpleNamespace(
                                    participant=types.ChannelParticipantCreator(
                                        user_id=creator.user_id,
                                        admin_rights=types.ChatAdminRights(other=True),
                                    )
                                )
                            return SimpleNamespace(participant=invalid_participant)
                        return SimpleNamespace()

                transport = Client()
                client = TelethonProvisioningClient.__new__(TelethonProvisioningClient)
                client._client = transport
                client._owners = {creator.user_id: creator}
                client._bots = {"hub_bot": hub}
                with self.assertRaises(ProvisioningUnknown):
                    asyncio.run(
                        client.configure_group(
                            CreatedForum(-1001234567890, 99),
                            hub_username="hub_bot",
                            provider_usernames=(),
                            before_rpc=lambda: None,
                        )
                    )
                self.assertFalse(
                    any(
                        type(item).__name__ in {"InviteToChannelRequest", "EditAdminRequest"}
                        for item in transport.requests
                    )
                )

    def test_lost_provider_membership_fails_final_readback(self) -> None:
        creator = types.InputUser(42, 420)
        hub = types.InputUser(100, 1000)
        provider = types.InputUser(101, 1010)

        class Client:
            def __init__(self) -> None:
                self.provider_reads = 0

            async def get_me(self) -> object:
                return SimpleNamespace(id=creator.user_id)

            async def get_entity(self, _entity: object) -> object:
                return SimpleNamespace(forum=True, megagroup=True, username=None, creator=True)

            async def __call__(self, request: object) -> object:
                if type(request).__name__ != "GetParticipantRequest":
                    return SimpleNamespace()
                if cast(Any, request).participant == creator:
                    return SimpleNamespace(
                        participant=types.ChannelParticipantCreator(
                            user_id=creator.user_id,
                            admin_rights=types.ChatAdminRights(other=True),
                        )
                    )
                if cast(Any, request).participant == hub:
                    return SimpleNamespace(
                        participant=types.ChannelParticipantAdmin(
                            user_id=hub.user_id,
                            promoted_by=creator.user_id,
                            date=None,
                            admin_rights=types.ChatAdminRights(manage_topics=True),
                        )
                    )
                self.provider_reads += 1
                if self.provider_reads == 1:
                    return SimpleNamespace(
                        participant=types.ChannelParticipant(user_id=provider.user_id, date=None)
                    )
                return SimpleNamespace(
                    participant=types.ChannelParticipantLeft(peer=types.PeerUser(provider.user_id))
                )

        client = TelethonProvisioningClient.__new__(TelethonProvisioningClient)
        client._client = Client()
        client._owners = {creator.user_id: creator}
        client._bots = {"hub_bot": hub, "provider_bot": provider}
        with self.assertRaises(ProvisioningUnknown):
            asyncio.run(
                client.configure_group(
                    CreatedForum(-1001234567890, 99),
                    hub_username="hub_bot",
                    provider_usernames=("provider_bot",),
                    before_rpc=lambda: None,
                )
            )

    def test_provider_invite_failure_retains_verified_owner_admin(self) -> None:
        creator = types.InputUser(42, 420)
        second_owner = types.InputUser(43, 430)
        hub = types.InputUser(100, 1000)
        provider = types.InputUser(101, 1010)

        class Client:
            def __init__(self) -> None:
                self.requests: list[object] = []
                self.owner_verified = False

            async def get_me(self) -> object:
                return SimpleNamespace(id=42)

            async def get_entity(self, _entity: object) -> object:
                return SimpleNamespace(forum=True, megagroup=True, username=None, creator=True)

            async def __call__(self, request: object) -> object:
                self.requests.append(request)
                name = type(request).__name__
                if name == "GetParticipantRequest":
                    participant = cast(Any, request).participant
                    if participant == creator:
                        return SimpleNamespace(
                            participant=types.ChannelParticipantCreator(
                                user_id=creator.user_id,
                                admin_rights=types.ChatAdminRights(other=True),
                            )
                        )
                    if participant == second_owner:
                        self.owner_verified = True
                        rights = types.ChatAdminRights(
                            change_info=True,
                            delete_messages=True,
                            ban_users=True,
                            invite_users=True,
                            pin_messages=True,
                            add_admins=True,
                            manage_call=True,
                            manage_topics=True,
                        )
                        return SimpleNamespace(
                            participant=types.ChannelParticipantAdmin(
                                user_id=second_owner.user_id,
                                promoted_by=creator.user_id,
                                date=None,
                                admin_rights=rights,
                            )
                        )
                    raise UserNotParticipantError(request)
                if name == "InviteToChannelRequest":
                    invited = cast(Any, request).users[0]
                    if invited == provider:
                        self.assert_owner_verified()
                        raise BadRequestError(request, "provider rejected")
                return SimpleNamespace()

            def assert_owner_verified(self) -> None:
                if not self.owner_verified:
                    raise AssertionError("provider invite preceded owner verification")

        transport = Client()
        client = TelethonProvisioningClient.__new__(TelethonProvisioningClient)
        client._client = transport
        client._owners = {42: creator, 43: second_owner}
        client._bots = {"hub_bot": hub, "provider_bot": provider}
        with self.assertRaises(ProvisioningRejected):
            asyncio.run(
                client.configure_group(
                    CreatedForum(-1001234567890, 99),
                    hub_username="hub_bot",
                    provider_usernames=("provider_bot",),
                    before_rpc=lambda: None,
                )
            )
        self.assertTrue(transport.owner_verified)

    def test_stop_after_first_invite_prevents_later_mutations(self) -> None:
        creator = types.InputUser(42, 420)
        second_owner = types.InputUser(43, 430)
        hub = types.InputUser(100, 1000)
        provider = types.InputUser(101, 1010)

        class Client:
            def __init__(self) -> None:
                self.requests: list[object] = []

            async def get_me(self) -> object:
                return SimpleNamespace(id=42)

            async def get_entity(self, _entity: object) -> object:
                return SimpleNamespace(forum=True, megagroup=True, username=None, creator=True)

            async def __call__(self, request: object) -> object:
                self.requests.append(request)
                if type(request).__name__ == "GetParticipantRequest":
                    return SimpleNamespace(
                        participant=types.ChannelParticipantCreator(
                            user_id=creator.user_id,
                            admin_rights=types.ChatAdminRights(other=True),
                        )
                    )
                return SimpleNamespace()

        transport = Client()
        client = TelethonProvisioningClient.__new__(TelethonProvisioningClient)
        client._client = transport
        client._owners = {42: creator, 43: second_owner}
        client._bots = {"hub_bot": hub, "provider_bot": provider}
        guard_calls = 0

        def guard() -> None:
            nonlocal guard_calls
            guard_calls += 1
            if guard_calls > 4:
                raise ProjectProvisionerStopping("stop")

        with self.assertRaises(ProjectProvisionerStopping):
            asyncio.run(
                client.configure_group(
                    CreatedForum(-1001234567890, 99),
                    hub_username="hub_bot",
                    provider_usernames=("provider_bot",),
                    before_rpc=guard,
                )
            )
        mutations = [
            item
            for item in transport.requests
            if type(item).__name__ in {"InviteToChannelRequest", "EditAdminRequest"}
        ]
        self.assertEqual([type(item).__name__ for item in mutations], ["InviteToChannelRequest"])

    def test_wrong_reconciled_group_is_rejected_before_mutation(self) -> None:
        creator = types.InputUser(42, 420)
        hub = types.InputUser(100, 1000)

        class Client:
            def __init__(self) -> None:
                self.requests: list[object] = []

            async def get_me(self) -> object:
                return SimpleNamespace(id=42)

            async def get_entity(self, _entity: object) -> object:
                return SimpleNamespace(forum=False, megagroup=True, username=None, creator=True)

            async def __call__(self, request: object) -> object:
                self.requests.append(request)
                return SimpleNamespace()

        transport = Client()
        client = TelethonProvisioningClient.__new__(TelethonProvisioningClient)
        client._client = transport
        client._owners = {42: creator}
        client._bots = {"hub_bot": hub}
        with self.assertRaises(ProvisioningRejected):
            asyncio.run(
                client.configure_group(
                    CreatedForum(-1001234567890, 99),
                    hub_username="hub_bot",
                    provider_usernames=(),
                    before_rpc=lambda: None,
                )
            )
        self.assertFalse(
            any(
                type(item).__name__ in {"InviteToChannelRequest", "EditAdminRequest"}
                for item in transport.requests
            )
        )

    def test_identity_mismatch_quarantines_a_private_session(self) -> None:
        class LoginClient:
            def __init__(self, path: str, *_args: object, **_kwargs: object) -> None:
                self.path = Path(path)

            async def start(self) -> None:
                return None

            async def get_me(self) -> object:
                return SimpleNamespace(id=999)

            async def disconnect(self) -> None:
                return None

        with patch("telethon.TelegramClient", LoginClient):
            with self.assertRaisesRegex(ProjectProvisioningError, "does not match"):
                asyncio.run(login_project_provisioner(self.config))
        self.assertFalse(self.session.exists())
        quarantined = tuple(self.base.glob("owner.rejected-*.session"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].stat().st_mode & 0o777, 0o600)
