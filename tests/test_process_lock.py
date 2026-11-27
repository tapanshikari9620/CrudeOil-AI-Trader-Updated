import os
import subprocess
import sys
from pathlib import Path

import pytest

from utils.process_lock import AlreadyRunningError, single_instance_lock


pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="The process lock uses Windows msvcrt locking.",
)


def test_second_process_cannot_acquire_existing_lock(tmp_path):
    lock_path = tmp_path / "paper_controller.lock"
    lock_literal = repr(str(lock_path))

    child_code = f"""
from pathlib import Path
from utils.process_lock import single_instance_lock, AlreadyRunningError

try:
    with single_instance_lock(Path({lock_literal})):
        raise SystemExit("FAIL: second process acquired the lock")
except AlreadyRunningError:
    print("PASS: second process rejected")
"""

    with single_instance_lock(lock_path):
        result = subprocess.run(
            [sys.executable, "-c", child_code],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=Path(__file__).resolve().parents[1],
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert "PASS: second process rejected" in result.stdout


def test_lock_is_released_after_normal_exit(tmp_path):
    lock_path = tmp_path / "paper_controller.lock"

    with single_instance_lock(lock_path):
        assert lock_path.exists()

    with single_instance_lock(lock_path):
        assert lock_path.exists()


def test_lock_is_released_after_exception(tmp_path):
    lock_path = tmp_path / "paper_controller.lock"

    with pytest.raises(RuntimeError, match="test exception"):
        with single_instance_lock(lock_path):
            raise RuntimeError("test exception")

    with single_instance_lock(lock_path):
        assert lock_path.exists()
