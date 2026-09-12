"""Adjacent punctuation cannot hide an independently wrong attachment marker."""

import pytest

from tools import synthetic_live_battery as battery

EXPECTED = "SYN-ATTACHMENT-0123456789abcdef0123"
FOREIGN = "SYN-ATTACHMENT-ffffffffffffffffffff"


@pytest.mark.parametrize("separator", [",", ";", ".", ")(", ":", "\n", " "])
def test_adjacent_foreign_marker_is_not_consumed_by_previous_token(separator):
    answer = EXPECTED + separator + FOREIGN
    assert not battery._closed_marker_exact(answer, EXPECTED, kind="ATTACHMENT", exact_once=False)


@pytest.mark.parametrize("suffix", ["_wrong", "x", "00"])
def test_suffix_after_valid_marker_is_not_an_accepted_competing_value(suffix):
    answer = EXPECTED + "," + EXPECTED + suffix
    assert not battery._closed_marker_exact(answer, EXPECTED, kind="ATTACHMENT", exact_once=False)


def test_word_prefix_cannot_hide_a_competing_marker():
    answer = EXPECTED + ",x_" + EXPECTED
    assert not battery._closed_marker_exact(answer, EXPECTED, kind="ATTACHMENT", exact_once=False)


@pytest.mark.parametrize("separator", [",", ";", " "])
def test_repeat_of_same_complete_value_keeps_identity_when_allowed(separator):
    answer = EXPECTED + separator + EXPECTED
    assert battery._closed_marker_exact(answer, EXPECTED, kind="ATTACHMENT", exact_once=False)
    assert not battery._closed_marker_exact(answer, EXPECTED, kind="ATTACHMENT", exact_once=True)
