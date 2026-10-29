"""NZBGet requires URL-grab filenames to end in .nzb."""

import pytest

from romarr.downloaders import NZBGet, NzbgetConfig


class FakeResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {"result": 123}


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Sonic the Hedgehog (USA)", "Sonic the Hedgehog (USA).nzb"),
        ("Sonic the Hedgehog (USA).nzb", "Sonic the Hedgehog (USA).nzb"),
        ("Sonic the Hedgehog (USA).NZB", "Sonic the Hedgehog (USA).NZB"),
        ("   ", "romarr.nzb"),
    ],
)
def test_nzbget_url_grab_name_has_nzb_suffix(name, expected):
    session = FakeSession()
    client = NZBGet(
        NzbgetConfig(base_url="http://nzbget:6789", category="roms-usenet"),
        session=session,
    )

    assert client.add("https://indexer.example/download?id=123", name=name)

    url, request = session.calls[0]
    assert url == "http://nzbget:6789/jsonrpc"
    assert request["json"]["method"] == "append"
    assert request["json"]["params"] == [
        expected,
        "https://indexer.example/download?id=123",
        "roms-usenet",
        0,
        False,
        False,
        "",
        0,
        "SCORE",
        [],
    ]
