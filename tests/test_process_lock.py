"""Tests for the pmrobot single-instance process lock."""

import os

import pytest

from utils.process_lock import AlreadyRunningError, SingleInstanceLock


def test_single_instance_lock_rejects_second_owner(tmp_path):
    path = tmp_path / "pmrobot.lock"
    first = SingleInstanceLock(str(path))
    second = SingleInstanceLock(str(path))

    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError, match=f"owner_pid={os.getpid()}"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()
