"""Nugget layup reads the whole tape, not only the segments that air (ISSUES 191).

A cut decides where one native clip ends and the next begins. The words that
fall between segments, and the words a cut trims off a kept segment, are not
heard on air, so they stay in the running for synthetic voiceover: the miner
sees them as spans, the tape (not the segment id) decides a nugget's air
status, and compose sees the exact words each seam dropped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from interview_mux.nugget_layup import (
    CORPUS_REL,
    _nuggets_spoken_by,
    build_between_segment_spans,
    build_corpus_mine_input,
    build_layup_compose_input,
    normalize_corpus_tape_status,
    rank_open_nuggets_for_target,
    segment_air_windows,
)
from run_fixtures import isolated_run_ctx


def _words(spec: list[tuple[int, str, str]], *, step: int = 400) -> list[dict]:
    """``(start_ms, speaker, sentence)`` rows -> one word per ``step`` ms."""
    out: list[dict] = []
    for start, spk, sentence in spec:
        t = start
        for tok in sentence.split():
            out.append({"text": tok, "start_ms": t, "end_ms": t + step - 50, "speaker_id": spk, "confidence": 0.95})
            t += step
    return out


SEG_001 = "We started with a protein bar and sold it in gyms"  # 0..4400
SEG_002 = "The real pivot was the packaging on the shelf"  # 8000..11600 (excluded)
SEG_003 = "Today it is an everyday healthy snack for families"  # 13000..16600
BETWEEN = "the packaging pivot came from one retailer"  # 5000..7800
TAIL = "thanks for having me"  # 17000..


def _seed(ctx) -> None:
    man = {
        "segments": [
            {"segment_id": "seg_001", "speaker_id": "spk_guest", "speaker_role": "interviewee", "type": "interviewee_answer", "start_ms": 0, "end_ms": 4400, "text": SEG_001, "topic_tags": []},
            {"segment_id": "seg_002", "speaker_id": "spk_guest", "speaker_role": "interviewee", "type": "interviewee_answer", "start_ms": 8000, "end_ms": 11600, "text": SEG_002, "topic_tags": []},
            {"segment_id": "seg_003", "speaker_id": "spk_guest", "speaker_role": "interviewee", "type": "interviewee_answer", "start_ms": 13000, "end_ms": 16600, "text": SEG_003, "topic_tags": []},
        ]
    }
    for rel, doc in (
        ("segments/manifest.json", man),
        ("master/selection.json", {"ordered_segment_ids": ["seg_001", "seg_003"], "excluded_segment_ids": [{"segment_id": "seg_002", "reason": "ranked_out"}]}),
        ("transcript/full.json", {"words": _words([(0, "spk_guest", SEG_001), (5000, "spk_guest", BETWEEN), (8000, "spk_guest", SEG_002), (13000, "spk_guest", SEG_003), (17000, "spk_guest", TAIL)])}),
        # seg_001 airs only its first 2.4 s; "and sold it in gyms" is cut off.
        ("understanding/ideal_cuts.json", {"cuts": [{"cut_id": "cut_001", "segment_id": "seg_001", "start_ms": 0, "end_ms": 2400, "must_keep": True}]}),
    ):
        path = ctx.path(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc))


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MUX_FORENSICS", "0")
    monkeypatch.setenv("MUX_ASSETS_ROOT", str(tmp_path))
    c = isolated_run_ctx(tmp_path, "full_tape_corpus")
    _seed(c)
    return c


def test_between_segment_spans_name_their_tape_neighbours(ctx) -> None:
    words = ctx.read_json("transcript/full.json")["words"]
    spans = build_between_segment_spans(words, ctx.read_json("segments/manifest.json")["segments"])
    assert [s["text"] for s in spans] == [BETWEEN, TAIL]
    first = spans[0]
    assert (first["prev_segment_id"], first["next_segment_id"]) == ("seg_001", "seg_002")
    assert first["start_ms"] == 5000 and first["in_selection"] is False
    assert first["span_id"] == f"gap_{first['start_ms']}_{first['end_ms']}"
    assert spans[1]["next_segment_id"] is None


def test_air_windows_follow_the_ideal_cut_inside_the_slab(ctx) -> None:
    windows = segment_air_windows(ctx)
    assert windows["seg_001"] == (0, 2400)
    assert windows["seg_003"] == (13000, 16600)


def test_mine_input_carries_off_air_words_and_between_spans(ctx) -> None:
    packet = build_corpus_mine_input(ctx)
    by_id = {r["segment_id"]: r for r in packet["segments"]}
    assert by_id["seg_001"]["air_start_ms"] == 0 and by_id["seg_001"]["air_end_ms"] == 2400
    assert "gyms" in by_id["seg_001"]["off_air_text"]
    assert "protein" not in by_id["seg_001"]["off_air_text"]
    assert "off_air_text" not in by_id["seg_002"]
    assert [s["text"] for s in packet["between_segment_spans"]] == [BETWEEN, TAIL]
    assert "between_segment_spans" in packet["full_tape_doctrine"]


def test_mine_input_never_truncates_a_long_segment_away(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    import interview_mux.nugget_layup as nl

    monkeypatch.setattr(nl, "merged_config", lambda: {"analysis": {"nugget_layup": {"segment_text_max_chars": 20}}})
    packet = build_corpus_mine_input(ctx)
    row = next(r for r in packet["segments"] if r["segment_id"] == "seg_001")
    assert len(row["text"]) <= 20
    rejoined = (row["text"].rstrip("…") + row["text_continued"]).replace(" ", "")
    assert rejoined == SEG_001.replace(" ", "")


def _corpus() -> dict:
    return {
        "nuggets": [
            # Between segments: the miner wrongly tied it to seg_001 as aired.
            {"nugget_id": "nug_between", "text_claim": "The pivot came from one retailer.", "evidence_quote": "came from one retailer", "source_start_ms": 5000, "source_end_ms": 7800, "source_segment_ids": [], "in_selection": True, "already_aired_in_selection": True, "salience": "high"},
            # Inside seg_001 but past its cut: not heard.
            {"nugget_id": "nug_gyms", "text_claim": "The bar was sold in gyms.", "evidence_quote": "sold it in gyms", "source_start_ms": 2400, "source_end_ms": 4000, "source_segment_ids": ["seg_001"], "in_selection": True, "already_aired_in_selection": True, "salience": "high"},
            # Inside seg_001's aired window: heard.
            {"nugget_id": "nug_bar", "text_claim": "They started with a protein bar.", "evidence_quote": "started with a protein bar", "source_start_ms": 0, "source_end_ms": 2000, "source_segment_ids": ["seg_001"], "in_selection": True, "already_aired_in_selection": True, "salience": "medium"},
            # Excluded segment, no span: id decides.
            {"nugget_id": "nug_shelf", "text_claim": "The packaging on the shelf was the pivot.", "evidence_quote": "packaging on the shelf", "source_segment_ids": ["seg_002"], "in_selection": True, "already_aired_in_selection": False, "salience": "high"},
        ]
    }


def test_the_tape_decides_air_status(ctx) -> None:
    out = normalize_corpus_tape_status(ctx, _corpus())
    by_id = {n["nugget_id"]: n for n in out["nuggets"]}
    assert by_id["nug_between"]["source_kind"] == "between_segments"
    assert by_id["nug_between"]["in_selection"] is False
    assert by_id["nug_between"]["already_aired_in_selection"] is False
    assert by_id["nug_between"]["source_segment_ids"] == []
    assert by_id["nug_gyms"]["source_kind"] == "off_air_span"
    assert by_id["nug_gyms"]["in_selection"] is False
    assert by_id["nug_gyms"]["already_aired_in_selection"] is False
    assert by_id["nug_bar"]["source_kind"] == "segment"
    assert by_id["nug_bar"]["in_selection"] is True
    assert by_id["nug_bar"]["already_aired_in_selection"] is True
    assert by_id["nug_shelf"]["in_selection"] is False
    assert any(w.startswith("tape_status_reopened_off_air_nuggets:2") for w in out["warnings"])


def test_a_span_with_no_ids_is_bound_to_the_slab_it_overlaps(ctx) -> None:
    corpus = {"nuggets": [{"nugget_id": "n", "text_claim": "c", "evidence_quote": "e", "source_start_ms": 8200, "source_end_ms": 9000, "in_selection": True}]}
    row = normalize_corpus_tape_status(ctx, corpus)["nuggets"][0]
    assert row["source_segment_ids"] == ["seg_002"]
    assert row["in_selection"] is False


def test_never_touch_tape_drops_a_between_segment_nugget_by_time(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    import interview_mux.media_ip_cta as mic

    monkeypatch.setattr(mic, "never_touch_segment_ids", lambda _ctx: set())
    monkeypatch.setattr(mic, "never_touch_source_intervals", lambda _ctx: [(4500, 8000, "cta")])
    out = mic.strip_never_touch_nuggets(ctx, _corpus())
    ids = [n["nugget_id"] for n in out["nuggets"]]
    assert "nug_between" not in ids
    assert {"nug_gyms", "nug_bar", "nug_shelf"} <= set(ids)
    assert mic.nugget_from_never_touch(ctx, _corpus()["nuggets"][0]) is True
    assert mic.nugget_from_never_touch(ctx, _corpus()["nuggets"][1]) is False


def test_a_nugget_past_the_cut_is_not_discharged_by_its_segment() -> None:
    nuggets = _corpus()["nuggets"]
    spoken = {n["nugget_id"] for n in _nuggets_spoken_by(nuggets, "seg_001", air_window=(0, 2400))}
    assert spoken == {"nug_bar"}
    assert {n["nugget_id"] for n in _nuggets_spoken_by(nuggets, "seg_001")} == {"nug_gyms", "nug_bar"}


def test_words_cut_at_this_seam_rank_up() -> None:
    nuggets = [
        {"nugget_id": "nug_far", "text_claim": "retailer pivot", "evidence_quote": "", "source_start_ms": 40000, "source_end_ms": 42000, "in_selection": False, "salience": "high"},
        {"nugget_id": "nug_seam", "text_claim": "retailer pivot", "evidence_quote": "", "source_start_ms": 5000, "source_end_ms": 7800, "in_selection": False, "salience": "high"},
    ]
    ranked = rank_open_nuggets_for_target("everyday snack", nuggets, tape_window=(2400, 13000))
    assert ranked[0]["nugget_id"] == "nug_seam" and ranked[0]["cut_at_this_seam"] is True
    assert "cut_at_this_seam" not in ranked[1]
    plain = rank_open_nuggets_for_target("everyday snack", nuggets)
    assert [r["nugget_id"] for r in plain] == ["nug_far", "nug_seam"]


def test_compose_sees_the_words_the_seam_dropped(ctx) -> None:
    path = ctx.path(*CORPUS_REL.split("/"))
    path.write_text(json.dumps(normalize_corpus_tape_status(ctx, _corpus())))
    packet = build_layup_compose_input(ctx)
    natives = {r["segment_id"]: r for r in packet["natives"]}
    assert "cut_words_before" not in natives["seg_001"]
    row = natives["seg_003"]
    assert row["cut_tape_window_ms"] == [2400, 13000]
    assert "gyms" in row["cut_words_before"]
    assert "retailer" in row["cut_words_before"]
    assert "shelf" in row["cut_words_before"]
    # nug_gyms sits past seg_001's cut: still open for seg_003's lay-up.
    assert "nug_gyms" not in row["already_aired_nugget_ids"]
    assert "nug_bar" in row["already_aired_nugget_ids"]
    open_ids = [r["nugget_id"] for r in row["open_nuggets_ranked"]]
    assert open_ids[0] in {"nug_between", "nug_gyms", "nug_shelf"}
    assert "nug_bar" not in open_ids
