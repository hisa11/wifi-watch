import pytest

from wifiwatch import channels as ch


def test_counts_match_spec():
    assert len(ch.FREQS_BY_BAND["2.4"]) == 13
    assert len(ch.FREQS_BY_BAND["5"]) == 20
    assert len(ch.FREQS_BY_BAND["6"]) == 24
    assert len(ch.ALL_FREQS) == 57


def test_numbers_collide_but_freqs_do_not():
    assert ch.parse_channel_ref("2g1") == 2412
    assert ch.parse_channel_ref("6g1") == 5955
    assert ch.parse_channel_ref("6GHz ch93") == 6415
    assert ch.parse_channel_ref(6135) == 6135
    assert ch.parse_channel_ref("5:144") == 5720


@pytest.mark.parametrize("bad", ["6g2", "6g38", "5g149", "2g14", 5000, True, "abc", "6g97"])
def test_rejects_unusable_channels(bad):
    with pytest.raises(ValueError):
        ch.parse_channel_ref(bad)


def test_psc():
    assert [c.number for c in ch.ALL_CHANNELS if c.is_psc] == [5, 21, 37, 53, 69, 85]


def test_blocks_6ghz():
    assert [b.label for b in ch.blocks("6", 160)] == [
        "6GHz ch1〜29（160MHz）", "6GHz ch33〜61（160MHz）", "6GHz ch65〜93（160MHz）"]
    b320 = ch.blocks("6", 320)
    assert [(b.freqs[0], b.freqs[-1]) for b in b320] == [(5955, 6255), (6115, 6415)]
    # どのchも、2本ある320MHzのどちらかには必ず含まれる
    for f in ch.FREQS_BY_BAND["6"]:
        assert ch.blocks_containing(f, 320)


def test_own_block():
    assert ch.own_block(ch.parse_channel_ref("6g37")).freqs == [ch.parse_channel_ref(f"6g{n}") for n in range(33, 62, 4)]
    assert ch.own_block(ch.parse_channel_ref("5g132")).width == 80  # 日本の5GHz上端は80MHzまで
    assert ch.own_block(ch.parse_channel_ref("2g6")).width == 20


def test_span_overlap_is_strict():
    a = ch.span20(6135)
    assert not a.overlaps(ch.span20(6155))  # 隣のchは端が接するだけ
    assert a.overlaps(ch.Span.around(6185, 160))
