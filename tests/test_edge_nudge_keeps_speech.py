"""Boundary edge nudges never cut speech out of a segment.

The nudge moves one side of one row. Shrinking a row over real words left
those words in no segment and opened the next edge mid-sentence (on the
47-minute granola transcript 148 words were lost this way).
"""

from interview_mux.boundary_collate import _phrase_in_span
from interview_mux.boundary_edge_score import _nudge_drops_speech


def _w(text: str, start: int, end: int) -> dict:
    return {"text": text, "start_ms": start, "end_ms": end}


WORDS = [
    _w("Sam,", 0, 300),
    _w("thank", 350, 600),
    _w("you", 620, 800),
    _w("um", 900, 1100),
    _w("for", 1200, 1400),
]


def test_shrink_over_real_words_is_refused() -> None:
    assert _nudge_drops_speech(WORDS, 0, 820) is True


def test_shrink_over_fillers_only_is_allowed() -> None:
    assert _nudge_drops_speech(WORDS, 820, 1150) is False


def test_next_side_phrase_is_the_head_after_the_cut() -> None:
    words = [_w(f"w{i}", i * 100, i * 100 + 80) for i in range(40)]
    assert _phrase_in_span(words, 0, 4000, head=True).split()[0] == "w0"
    assert _phrase_in_span(words, 0, 4000).split()[-1] == "w39"
