from __future__ import annotations

import pytest

from core import frequency


@pytest.mark.parametrize(
    "mark,space,rate",
    [(1200, 2200, 48000), (100, 200, 1000), (1, 2, 6)],
)
def test_frequency_validate_accepts_distinct_positive_sub_nyquist_tones(mark, space, rate):
    assert frequency.validate(mark, space, rate) is True


@pytest.mark.parametrize(
    "mark,space,rate",
    [
        (0, 100, 48000),
        (100, 0, 48000),
        (-1, 100, 48000),
        (100, -1, 48000),
        (1000, 1000, 48000),
        (24000, 1200, 48000),
        (1200, 24000, 48000),
        (100, 200, 400),
    ],
)
def test_frequency_validate_rejects_invalid_frequency_plans(mark, space, rate):
    with pytest.raises(ValueError, match="Invalid FSK frequency plan"):
        frequency.validate(mark, space, rate)
