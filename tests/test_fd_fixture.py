"""Descriptor assertions cannot take cleanup authority from the tested caller."""

from __future__ import annotations

import os
import threading
import unittest

from tests.fd_fixture import assert_descriptor_cleanup


class DescriptorFixtureTests(unittest.TestCase):
    def test_tracked_open_dup_and_dup2_are_reported_but_never_closed(self) -> None:
        for operation in ("open", "dup", "dup2"):
            with self.subTest(operation=operation):
                source = os.open("/dev/null", os.O_RDONLY)
                target = os.open("/dev/zero", os.O_RDONLY)
                created = -1
                try:
                    with self.assertRaisesRegex(AssertionError, "calling-thread descriptors"):
                        with assert_descriptor_cleanup(self):
                            if operation == "open":
                                created = os.open("/dev/null", os.O_RDONLY)
                            elif operation == "dup":
                                created = os.dup(source)
                            else:
                                created = os.dup2(source, target)
                    os.fstat(created)
                finally:
                    if created >= 0 and created != target:
                        os.close(created)
                    os.close(source)
                    os.close(target)

    def test_primary_exception_keeps_precedence_and_descriptor_stays_caller_owned(self) -> None:
        created = -1
        primary = RuntimeError("fictional primary failure")
        try:
            with self.assertRaises(RuntimeError) as raised:
                with assert_descriptor_cleanup(self):
                    created = os.open("/dev/null", os.O_RDONLY)
                    raise primary
            self.assertIs(raised.exception, primary)
            self.assertTrue(any("calling-thread descriptors" in n for n in primary.__notes__))
            os.fstat(created)
        finally:
            if created >= 0:
                os.close(created)

    def test_known_foreign_thread_allocation_is_neither_asserted_nor_closed(self) -> None:
        ready, finish = threading.Event(), threading.Event()
        descriptors: list[int] = []

        def foreign() -> None:
            fd = os.open("/dev/null", os.O_RDONLY)
            descriptors.append(fd)
            ready.set()
            try:
                finish.wait(5)
            finally:
                os.close(fd)

        thread = threading.Thread(target=foreign)
        try:
            with assert_descriptor_cleanup(self):
                thread.start()
                self.assertTrue(ready.wait(2))
            os.fstat(descriptors[0])
        finally:
            finish.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_pipe_leaks_are_reported_and_never_closed(self) -> None:
        descriptors: tuple[int, int] | None = None
        try:
            with self.assertRaisesRegex(AssertionError, "calling-thread descriptors"):
                with assert_descriptor_cleanup(self):
                    descriptors = os.pipe()
            for fd in descriptors:
                os.fstat(fd)
        finally:
            if descriptors is not None:
                for fd in descriptors:
                    os.close(fd)

    @unittest.skipUnless(hasattr(os, "memfd_create"), "Python memfd allocator is unavailable")
    def test_memfd_leak_is_reported_and_never_closed(self) -> None:
        descriptor = -1
        try:
            with self.assertRaisesRegex(AssertionError, "calling-thread descriptors"):
                with assert_descriptor_cleanup(self):
                    descriptor = os.memfd_create("example-fixture", os.MFD_CLOEXEC)
            os.fstat(descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def test_untracked_stream_difference_is_uncertainty_and_never_cleanup_authority(self) -> None:
        stream = None
        try:
            with self.assertWarnsRegex(ResourceWarning, "ownership unknown"):
                with assert_descriptor_cleanup(self):
                    stream = open("/dev/null", "rb")
            os.fstat(stream.fileno())
        finally:
            if stream is not None:
                stream.close()

    def test_unobserved_same_inode_reuse_never_closes_replacement(self) -> None:
        replacement = None
        try:
            with self.assertRaises(AssertionError):
                with assert_descriptor_cleanup(self):
                    fd = os.open("/dev/null", os.O_RDONLY)
                    with os.fdopen(fd, "rb"):
                        pass  # FileIO close bypasses the os.close tracker.
                    replacement = open("/dev/null", "rb")
                    self.assertEqual(replacement.fileno(), fd)
            os.fstat(replacement.fileno())
        finally:
            if replacement is not None:
                replacement.close()

    def test_unrelated_closure_and_closed_tracked_allocations_are_accepted(self) -> None:
        old = os.open("/dev/null", os.O_RDONLY)
        with assert_descriptor_cleanup(self):
            os.close(old)
            fd = os.open("/dev/zero", os.O_RDONLY)
            os.close(fd)
