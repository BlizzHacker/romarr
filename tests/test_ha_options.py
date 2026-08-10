import json

from romarr.__main__ import load_ha_options


def test_options_become_uppercase_environment(tmp_path, monkeypatch):
    path = tmp_path / "options.json"
    path.write_text(json.dumps({
        "prowlarr_url": "http://prowlarr:9696",
        "romarr_password": "hunter2",
        "update_check": True,
        "port_count": 2,
    }))
    out = load_ha_options(str(path))
    assert out == {
        "PROWLARR_URL": "http://prowlarr:9696",
        "ROMARR_PASSWORD": "hunter2",
        "UPDATE_CHECK": "true",
        "PORT_COUNT": "2",
    }


def test_the_real_environment_wins_over_the_file(tmp_path, monkeypatch):
    """A variable someone set on the container is the more deliberate act."""
    path = tmp_path / "options.json"
    path.write_text(json.dumps({"prowlarr_url": "http://from-file"}))
    monkeypatch.setenv("PROWLARR_URL", "http://from-env")
    assert "PROWLARR_URL" not in load_ha_options(str(path))


def test_empty_and_structured_values_are_ignored(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(json.dumps({"a": "", "b": {"nested": 1}, "c": [1, 2]}))
    assert load_ha_options(str(path)) == {}


def test_no_file_means_no_options_and_no_error(tmp_path):
    assert load_ha_options(str(tmp_path / "absent.json")) == {}


def test_a_corrupt_file_is_silently_nothing(tmp_path):
    path = tmp_path / "options.json"
    path.write_text("{not json")
    assert load_ha_options(str(path)) == {}
