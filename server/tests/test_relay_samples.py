"""testdata/relay の見本（PC側・中継・アプリで共用）と、PC側の形が合っているか。"""
import json
from pathlib import Path

from tests.conftest import load_testdata
from wifiwatch.app import build
from wifiwatch.clock import FakeClock
from wifiwatch.config import parse_config
from wifiwatch.radio.fake import FakeRadio, demo_scenario

ROOT = Path(__file__).resolve().parents[2]


def make(tmp_path):
    d = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    d["data_dir"] = str(tmp_path / "data")
    d["signatures"] = str(ROOT / "signatures.json")
    clk = FakeClock()
    app = build(parse_config(d), clk, FakeRadio(clk, demo_scenario()))
    app.monitor.run_for(20)
    return app


def test_state_copy_shape_matches_sample(tmp_path):
    app = make(tmp_path)
    sample = load_testdata("relay/pc_state.json")
    st = app.controller.compact_state()
    assert set(st) == set(sample)
    assert set(st["own"]) == set(sample["own"])
    assert len(st["channels"]) == len(sample["channels"]) == 57
    assert all(len(row) == 5 for row in sample["channels"])
    assert len(json.dumps(sample, ensure_ascii=False).encode()) <= 8 * 1024
    app.store.close()


def test_unreceivable_channels_reach_state_copy(tmp_path):
    # PCの無線が6GHz非対応（開発PCでスキャンだけのモード）なら、受けられないchを状態の写しに添える
    app = make(tmp_path)
    assert "unreceivable" not in app.controller.compact_state()["bands"]  # ふだん（AX210）は付けない
    six = [f for f in range(5955, 6416, 20)]
    app.monitor.radio.unreceivable = six
    st = app.controller.compact_state()
    assert st["bands"]["unreceivable"] == six
    assert app.controller.state()["radio"]["unreceivable"] == six
    assert len(json.dumps(st, ensure_ascii=False).encode()) <= 8 * 1024
    app.store.close()


def test_samples_from_app_are_accepted(tmp_path):
    app = make(tmp_path)
    walk = load_testdata("relay/app_walk.json")
    r = app.controller.ingest_record(walk["record"], "app")
    assert r["new"]
    assert app.store.get_record(walk["record"]["id"])["label"]["location"] == "フィールド北"
    lab = load_testdata("relay/app_label.json")
    res = app.controller.update_label(walk["record"]["id"], lab["label"], "app", lab["edited_at"], lab["request_id"])
    assert res["applied"] and res["label"]["tags"] == ["試合前", "下見"]
    b = load_testdata("relay/app_command_bands.json")
    app.controller.set_bands(b["params"]["excluded_bands"], b["params"]["excluded_freqs"], "app", b["request_id"])
    assert app.monitor.scheduler.excluded_bands == {"2.4"}
    s = load_testdata("relay/app_command_scan.json")
    out = app.controller.manual_scan(s["params"]["bands"], None, s["params"]["confirm"], "app", s["request_id"])
    assert out["total"] == 24
    app.store.close()


def test_record_copy_sample_round_trips(tmp_path):
    app = make(tmp_path)
    rec = load_testdata("relay/pc_record.json")
    assert rec["kind"] == "pc_scan" and rec["stations"] and "label" in rec
    app.store.upsert_record(rec, 1.0)
    got = app.store.get_record(rec["id"])
    assert got["stations"] == rec["stations"]
    app.store.close()
