"""NAS reads must stream, retry temporary contention, and expose partial failures."""

import errno
from types import SimpleNamespace

import pytest

from romarr.app import ROMarr, _read_failure
from romarr.libraries import FolderConfig, FolderLibrary, Game


def test_first_game_does_not_walk_the_rest_of_the_share(tmp_path, monkeypatch):
    """A first page must be usable before visiting unrelated platform folders."""
    (tmp_path / "first.sfc").write_bytes(b"rom")
    (tmp_path / "later").mkdir()
    lib = FolderLibrary(FolderConfig(root=str(tmp_path)))
    real = lib._directory_entries
    visited = []

    def entries(path):
        """Reject any unnecessary descent after the first matching game."""
        visited.append(path)
        if path != tmp_path:
            raise AssertionError("walked unrelated subtree before first result")
        return real(path)

    monkeypatch.setattr(lib, "_directory_entries", entries)
    assert len(list(lib.iter_games(limit=1))) == 1
    assert visited == [tmp_path]


def test_busy_directory_resumes_without_duplicate_games(tmp_path, monkeypatch):
    """A temporary error midway through readdir must not lose or duplicate files."""
    for name in ("A.sfc", "B.sfc"):
        (tmp_path / name).write_bytes(b"rom")
    from romarr import libraries
    original = libraries.os.scandir
    entries = list(original(tmp_path))
    calls = []

    class InterruptedDirectory:
        """Yield one real entry, then simulate a busy NAS directory stream."""

        def __enter__(self):
            """Match the scandir context manager."""
            return self

        def __exit__(self, *args):
            """Release the synthetic iterator."""

        def __iter__(self):
            """Raise the transient error only after the first entry was delivered."""
            yield entries[0]
            raise BlockingIOError(errno.EAGAIN, "busy")

    def scandir(path):
        """Fail once midstream, then reopen the actual directory."""
        calls.append(path)
        return InterruptedDirectory() if len(calls) == 1 else original(path)

    monkeypatch.setattr(libraries.os, "scandir", scandir)
    monkeypatch.setattr(libraries.time, "sleep", lambda seconds: None)
    games = list(FolderLibrary(FolderConfig(root=str(tmp_path))).iter_games())
    assert {g.name for g in games} == {"A", "B"}
    assert len(games) == 2
    assert len(calls) == 2


@pytest.mark.parametrize("error, attempts", [
    (BlockingIOError(errno.EAGAIN, "busy"), 3),
    (PermissionError(errno.EACCES, "denied"), 1),
])
def test_storage_retry_budget_does_not_hide_failure(error, attempts, monkeypatch):
    """Only temporary contention is retried; a failed scan never becomes empty."""
    calls = []

    def fail():
        """Record each actual filesystem attempt."""
        calls.append(True)
        raise error

    monkeypatch.setattr("romarr.libraries.time.sleep", lambda seconds: None)
    with pytest.raises(type(error)):
        FolderLibrary._fs_call(fail)
    assert len(calls) == attempts


class EndRefresh(Exception):
    """Stop the refresh loop at its normal wait boundary."""


def scan_once(backend):
    """Run the real background loop without threads or user configuration."""
    service = ROMarr.__new__(ROMarr)
    service.game_libraries = [({"name": "NAS"}, backend)]
    service._library_cache = (None, 0, "")
    service._library_scan = {}
    service.LIBRARY_PAGE = 2
    service.LIBRARY_MAX = 100
    published = []

    def publish(shelf, error, *, partial):
        """Capture the actual snapshots served while the stream advances."""
        published.append((list(shelf), error, partial))
        service._library_cache = (list(shelf), 1, error)

    def wait(seconds):
        """End after exactly one refresh cycle."""
        raise EndRefresh()

    service._publish_library = publish
    service._wait = wait
    with pytest.raises(EndRefresh):
        service._refresh_counts()
    return service, published


def test_background_folder_refresh_reads_once_without_count_or_offset_walks():
    """Many pages must consume one iterator rather than walking every prefix."""
    calls = []

    def stream(limit):
        """Track the one traversal and yield enough games for multiple pages."""
        calls.append(limit)
        for n in range(5):
            yield Game(id=str(n), name=f"Game {n}", platform="snes")

    backend = SimpleNamespace(iter_games=stream, name="NAS")
    service, published = scan_once(backend)
    assert calls == [100]
    assert [len(rows) for rows, _, _ in published] == [2, 4, 5, 5]
    assert service._count_split == {"total": 5, "on_disk": 5, "catalogued": 0}
    assert not published[-1][2]
    scan, message = service._library_scan_status()
    assert scan["read"] == 5 and scan["stage"] == "complete"
    assert not scan["running"]


def test_failed_scan_retains_published_rows_and_marks_them_partial():
    """An unreadable subtree must not masquerade as a complete empty library."""
    def stream(limit):
        """Deliver one complete batch before a permanent permission failure."""
        yield Game(id="1", name="A", platform="snes")
        yield Game(id="2", name="B", platform="snes")
        raise PermissionError(errno.EACCES, "private-path-must-not-leak")

    service, published = scan_once(SimpleNamespace(iter_games=stream, name="NAS"))
    assert len(published[-1][0]) == 2 and published[-1][2]
    scan, message = service._library_scan_status()
    assert scan["stage"] == "error" and not scan["running"]
    assert "private-path-must-not-leak" not in repr((scan, message, published[-1][1]))
    assert "read permissions" in service._library_reasons["NAS"]


def test_mount_failure_has_actionable_guidance_without_exception_details():
    """Give the user a mount/file-sharing check instead of only a class name."""
    assert "Docker file sharing" in _read_failure(BlockingIOError("private-detail"))
    assert "private-detail" not in _read_failure(BlockingIOError("private-detail"))


def test_scan_limit_is_visible_and_iterator_is_closed():
    """Stopping at the configured cap must not claim a complete collection."""
    closed = []

    def stream(limit):
        """Honor the supplied scan cap and record generator cleanup."""
        try:
            for n in range(limit):
                yield Game(id=str(n), name=f"Game {n}", platform="snes")
        finally:
            closed.append(True)

    service, published = scan_once(SimpleNamespace(iter_games=stream, name="NAS"))
    scan, message = service._library_scan_status()
    assert scan["limit_reached"] and "more files may remain" in message
    assert published[-1][2] and closed == [True]


def test_progress_status_never_reads_the_filesystem(monkeypatch):
    """A slow or busy mount must not hold the polling API open."""
    service = ROMarr.__new__(ROMarr)
    service._library_scan = {"running": True, "started": 10, "read": 1234,
                             "stage": "reading", "library": "NAS"}
    monkeypatch.setattr("romarr.app.time.monotonic", lambda: 25)
    scan, message = service._library_scan_status()
    assert scan["elapsed_seconds"] == 15 and scan["read"] == 1234
    assert "1,234 read" in message
    assert "started" not in scan and "finished" not in scan


@pytest.mark.parametrize("readable", [1, 3])
def test_failure_preserves_unfinished_first_page_and_later_tail(readable):
    """Keep every readable game even when a subtree fails in the middle of a page."""
    def stream(limit):
        """Fail before the next complete page can be assembled."""
        for n in range(readable):
            yield Game(id=str(n), name=f"Game {n}", platform="snes")
        raise PermissionError(errno.EACCES, "denied")

    service, published = scan_once(SimpleNamespace(iter_games=stream, name="NAS"))
    rows = service._library_cache[0]
    assert rows is not None and len(rows) == readable
    assert {g.id for g in rows} == {str(n) for n in range(readable)}
    assert published[-1][2]
    assert service._library_scan["read"] == readable


def test_unreadable_platform_does_not_hide_readable_siblings(tmp_path, monkeypatch):
    """Visit later platforms and aggregate failures after yielding their games."""
    from romarr.libraries import FolderScanError
    for folder in ("a-denied", "b-readable", "c-readable"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "Game.sfc").write_bytes(b"rom")
    lib = FolderLibrary(FolderConfig(root=str(tmp_path)))
    original = lib._directory_entries

    def entries(path):
        """Force the denied directory first, regardless of filesystem ordering."""
        if path.name == "a-denied":
            raise PermissionError(errno.EACCES, "private mount details")
        return sorted(original(path), key=lambda entry: entry.name)

    monkeypatch.setattr(lib, "_directory_entries", entries)
    readable = []
    with pytest.raises(FolderScanError) as failure:
        for game in lib.iter_games():
            readable.append(game)
    assert {g.platform for g in readable} == {"b-readable", "c-readable"}
    assert failure.value.folder_failures == ("PermissionError",)
    assert "private mount" not in str(failure.value)
