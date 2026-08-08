"""A library that answers is not necessarily a library that works.

Found on a live install. RomM's credentials had expired: `/api/heartbeat`
answered 200, `/api/roms` returned 403, and the Libraries page said
"connected". The nav badge showed no game count, the Library page was empty,
and nothing anywhere said why.

The heartbeat is deliberately unauthenticated -- fetching a token first would
make a health check as slow as whatever is wrong with the server, which is the
thing it exists to report on. That is a good decision. Presenting its answer as
a verdict on whether the library works is not.
"""

from __future__ import annotations

import requests

from romarr.app import ROMarr, _read_failure


class _Rejecting:
    """Answers its heartbeat, refuses every read. The live failure exactly."""

    name = "RomM"
    BACKGROUND_TIMEOUT = 5

    def __init__(self, status=403):
        self.status = status

    @property
    def configured(self):
        return True

    def reachable(self):
        return True

    def _refuse(self):
        response = requests.Response()
        response.status_code = self.status
        raise requests.HTTPError("Forbidden", response=response)

    def count(self):
        self._refuse()

    def games(self, limit=60, offset=0, timeout=None):
        self._refuse()

    def rescan(self, platform_slug=None):
        return False


def _service(tmp_path, backend):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})
    service.game_libraries = [({"name": "RomM", "type": "romm",
                                "path": str(tmp_path)}, backend)]
    return service


def test_a_library_that_refuses_reads_is_not_reported_as_fine(tmp_path):
    service = _service(tmp_path, _Rejecting(403))
    # One refresh pass, as the background thread would do.
    service._library_reasons = {"RomM": _read_failure(
        requests.HTTPError("x", response=_response(403)))}

    row = service.libraries_status()[0]
    assert row["ok"] is True, "the heartbeat did answer"
    assert row["readable"] is False, "but nothing can be read from it"
    assert "credentials" in row["detail"].lower()


def _response(status):
    response = requests.Response()
    response.status_code = status
    return response


def test_a_healthy_library_says_nothing_extra(tmp_path):
    class Fine(_Rejecting):
        def count(self):
            return 7

        def games(self, limit=60, offset=0, timeout=None):
            return []

    service = _service(tmp_path, Fine())
    row = service.libraries_status()[0]
    assert row["readable"] is True
    assert row["detail"] == ""


def test_401_and_403_name_credentials_rather_than_an_exception_class():
    """"HTTPError" tells an operator nothing they can act on."""
    for status in (401, 403):
        message = _read_failure(requests.HTTPError("x", response=_response(status)))
        assert "credentials" in message.lower()
        assert str(status) in message


def test_other_statuses_are_reported_as_themselves():
    message = _read_failure(requests.HTTPError("x", response=_response(503)))
    assert "503" in message
    assert "credentials" not in message.lower()


def test_a_failure_with_no_response_still_says_something():
    assert _read_failure(TimeoutError("slow")) == "TimeoutError"


def test_the_reason_is_per_library_not_shared(tmp_path):
    """With two libraries, "something failed" is not an answer."""
    good = _Rejecting()
    good.count = lambda: 3
    good.games = lambda **kw: []
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})
    service.game_libraries = [
        ({"name": "Good", "type": "folder", "path": str(tmp_path)}, good),
        ({"name": "Broken", "type": "romm", "path": str(tmp_path)}, _Rejecting()),
    ]
    service._library_reasons = {"Broken": "credentials rejected (HTTP 403)."}

    rows = {r["name"]: r for r in service.libraries_status()}
    assert rows["Good"]["readable"] is True
    assert rows["Broken"]["readable"] is False
    assert "403" in rows["Broken"]["detail"]
