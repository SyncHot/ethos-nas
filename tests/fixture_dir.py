"""Shared fixture helpers for the standalone tests/test_*.py scripts.

The production server runs as the dedicated `ethos-nas` service user
(setup_scripts/setup_service.sh, docs/service-user.md) while the test
scripts run as the login user. `tempfile.mkdtemp()` creates a mode-700
directory owned by the login user — a cross-user server process cannot
enter it, so any test that hands such a path to the server fails with
"[Errno 13] Permission denied", "Path not found", "Not a directory", or a
WS 1011 close (T-0003: test_notifications, test_share_links, test_editor,
test_terminal_ws, test_gallery).

Use `service_readable_tmpdir()` instead of bare `tempfile.mkdtemp()`
everywhere the SERVER (not just the test process) must read or write into
the fixture. Files created inside inherit the process umask (022 →
world-readable), so only the top-level directory needs the mode bump.
"""
import os
import tempfile


def service_readable_tmpdir(prefix: str = "ethos-test-") -> str:
    """mkdtemp + chmod 755 so the ethos-nas service user can traverse it.

    Returns the directory path (str, like tempfile.mkdtemp). The test
    process keeps full owner access; other users (the service) gain
    read+execute, which is all the server needs for read-only fixtures
    (share link targets, gallery libraries, editor repos).
    """
    d = tempfile.mkdtemp(prefix=prefix)
    os.chmod(d, 0o755)
    return d


def service_writable_tmpdir(prefix: str = "ethos-test-") -> str:
    """mkdtemp + chmod 777 for fixtures the SERVER must write into.

    Use for download targets and similar server-side writes. 777 (rather
    than 775 + group membership) is deliberate: the service user's group
    membership is not guaranteed by the installer, so only world-writable
    is deterministic. The fixture is an ephemeral /tmp dir that the test
    removes at the end; the exposed window is the test run itself.
    """
    d = tempfile.mkdtemp(prefix=prefix)
    os.chmod(d, 0o777)
    return d
