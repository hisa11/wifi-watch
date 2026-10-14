import json
from pathlib import Path

import pytest

from wifiwatch.config import ConfigError, Overrides, load_config, parse_config

ROOT = Path(__file__).resolve().parents[2]


def example():
    return json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))


def test_example_is_valid(tmp_path):
    cfg = parse_config(example())
    assert cfg.own.channel == 6135
    assert cfg.own.spare == [6155]
    assert cfg.teams[0].channels == [5975]
    assert cfg.api.host == "127.0.0.1"
    assert cfg.reservations()["自チーム"] == {6135, 6155}


def test_load_resolves_paths(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(example()), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.data_dir == str(tmp_path / "data")


def _errors(data):
    with pytest.raises(ConfigError) as e:
        parse_config(data)
    return "\n".join(e.value.problems)


def test_bad_channel_is_explained():
    d = example()
    d["own"]["channel"] = "6g38"
    msg = _errors(d)
    assert "own.channel" in msg and "6g38" in msg and "4おき" in msg


def test_unknown_key_and_missing():
    d = example()
    d["own"]["chanel"] = 1
    del d["own"]["team"]
    msg = _errors(d)
    assert "own.chanel: 知らない項目" in msg
    assert "own.team: 必須の項目がありません" in msg


def test_api_must_be_loopback():
    d = example()
    d["api"]["host"] = "0.0.0.0"
    assert "127.0.0.1" in _errors(d)


def test_width_must_fit_channel():
    d = example()
    d["own"]["channel"] = "5g132"
    d["own"]["width"] = 160
    assert "own" in _errors(d)


def test_relay_token_required_and_https():
    d = example()
    d["relay"]["url"] = "http://example.com"
    assert "https" in _errors(d)
    d["relay"]["url"] = "https://example.vercel.app"
    assert "pc_token" in _errors(d)
    d["relay"]["pc_token"] = "x" * 32
    parse_config(d)
    d["relay"]["url"] = "http://127.0.0.1:8787"
    parse_config(d)


def test_short_topic_rejected():
    d = example()
    d["notify"]["ntfy_topic"] = "robocon"
    assert "16文字" in _errors(d)


def test_bad_ifname():
    d = example()
    d["interface"] = "wlan0; rm -rf /"
    assert "IF名" in _errors(d)


def test_overrides_persist(tmp_path):
    o = Overrides(tmp_path / "overrides.json")
    o.update(excluded_bands=["2.4", "bogus"], excluded_freqs=[2412, 1234], mode="QUIET")
    o2 = Overrides(tmp_path / "overrides.json")
    assert o2.get("excluded_bands") == ["2.4"]
    assert o2.get("excluded_freqs") == [2412]
    assert o2.get("mode") == "QUIET"


def test_overrides_broken_file_falls_back(tmp_path):
    p = tmp_path / "overrides.json"
    p.write_text("{broken", encoding="utf-8")
    o = Overrides(p)
    assert o.get("mode") == "LIVE"
    assert (tmp_path / "overrides.broken.json").exists()
