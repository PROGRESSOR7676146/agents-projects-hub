"""Versioned socket-thread observations cannot impersonate stronger proof."""

from __future__ import annotations

import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from hermes_codex_router import worker_control_socket as control
from hermes_codex_router.worker_drain_protocol import ControlError, decode_document, status_request
from tests import test_worker_control_socket as fixtures


class SocketObservationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkerControlSocketTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def endpoint(self, observation):
        f = self.fixture
        endpoint = control.WorkerControlEndpoint(
            f.path, f.binding, f.activity, socket_observation=observation
        )
        endpoint.start()
        self.addCleanup(endpoint.close)
        return endpoint

    def test_inspect_returns_no_socket_proof_and_does_not_call_the_observer(self):
        observer = Mock(return_value=True)
        self.endpoint(observer)
        result = control.inspect_control(self.fixture.path)
        self.assertEqual(result["version"], 2)
        self.assertIsNone(result["socket_threads_drained"])
        observer.assert_not_called()

    def test_true_socket_observer_cannot_outrun_an_admitted_operation(self):
        self.endpoint(lambda: True)
        f = self.fixture
        with f.activity.operation():
            result = control.exchange_control(f.path, f.binding, f.request())
        self.assertFalse(result["operations_drained"])
        self.assertIs(result["socket_threads_drained"], False)
        self.assertEqual(result["scope"], "tracked_operations_and_socket_transport_threads")

    def test_socket_observation_is_independent_and_accepts_delayed_exit(self):
        observer = Mock(return_value=False)
        self.endpoint(observer)
        f = self.fixture
        request = f.request()
        result = control.exchange_control(f.path, f.binding, request)
        self.assertTrue(result["operations_drained"])
        self.assertIs(result["socket_threads_drained"], False)
        observer.return_value = True
        result = control.exchange_control(f.path, f.binding, status_request(request))
        self.assertIs(result["socket_threads_drained"], True)
        self.assertEqual(result["request_digest"], control.request_digest(request))

    def test_unsupported_owner_stays_null_after_operation_drain(self):
        self.endpoint(lambda: None)
        f = self.fixture
        result = control.exchange_control(f.path, f.binding, f.request())
        self.assertTrue(result["operations_drained"])
        self.assertIsNone(result["socket_threads_drained"])

    def test_default_endpoint_keeps_socket_proof_null_after_acceptance(self):
        self.fixture.endpoint()
        f = self.fixture
        result = control.exchange_control(f.path, f.binding, f.request())
        self.assertTrue(result["operations_drained"])
        self.assertIsNone(result["socket_threads_drained"])

    def test_invalid_observer_types_refuse_without_reopening_or_killing_listener(self):
        observer = Mock(return_value=True)
        endpoint = self.endpoint(observer)
        f = self.fixture
        request = f.request()
        for value in (1, "true", {}):
            with self.subTest(value=value):
                observer.return_value = value
                with self.assertRaises(ControlError):
                    control.exchange_control(f.path, f.binding, request)
                self.assertTrue(f.activity.draining)
                self.assertTrue(endpoint.alive)
                observer.return_value = True
                result = control.exchange_control(f.path, f.binding, status_request(request))
                self.assertTrue(result["accepted"])
                self.assertIs(result["socket_threads_drained"], True)

    def test_client_refuses_malformed_or_contradictory_socket_proof(self):
        self.endpoint(lambda: True)
        f = self.fixture
        original_send = control._send_frame
        change = {}

        def send(connection, response):
            response = deepcopy(response)
            response.update(change)
            original_send(connection, response)

        request = f.request()
        with patch.object(control, "_send_frame", send):
            for change in (
                {"socket_threads_drained": 1},
                {"socket_threads_drained": "true"},
                {"socket_threads_drained": {}},
                {"operations_drained": False, "socket_threads_drained": True},
                {
                    "accepted": False,
                    "operations_drained": False,
                    "request_id": None,
                    "request_digest": None,
                    "socket_threads_drained": False,
                },
            ):
                with self.subTest(change=change), self.assertRaises(ControlError):
                    control.exchange_control(f.path, f.binding, request)

    def test_inspect_refuses_nonnull_socket_proof_before_acceptance(self):
        self.endpoint(lambda: True)
        original_send = control._send_frame

        def send(connection, response):
            response = deepcopy(response)
            response["socket_threads_drained"] = False
            original_send(connection, response)

        with (
            patch.object(control, "_send_frame", send),
            self.assertRaisesRegex(ControlError, "control_unconfirmed"),
        ):
            control.inspect_control(self.fixture.path)
        self.assertFalse(self.fixture.activity.draining)

    def test_version_one_request_and_document_are_refused_before_connection(self):
        f = self.fixture
        request = f.request()
        request["version"] = 1
        with patch.object(control.socket, "socket") as socket:
            with self.assertRaises(ControlError):
                control.exchange_control(f.path, f.binding, request)
        socket.assert_not_called()
        with self.assertRaises(ControlError):
            decode_document(b'{"version":1,"operation":"inspect"}')
        self.assertFalse(f.activity.draining)
