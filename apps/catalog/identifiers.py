"""Normalization and validation helpers for global product identifiers."""

from __future__ import annotations

import re


GTIN_LENGTHS = {8, 12, 13, 14}


def normalize_gtin(value) -> str | None:
    """Return a canonical, checksum-valid GTIN or ``None``.

    GTINs are identifiers rather than numbers, so leading zeroes are preserved.
    Spaces and hyphens commonly introduced by feeds are accepted, while other
    characters and invalid check digits are rejected.
    """
    if value is None:
        return None
    candidate = re.sub(r"[\s-]+", "", str(value).strip())
    if len(candidate) not in GTIN_LENGTHS or not candidate.isdigit():
        return None

    payload, supplied_check = candidate[:-1], int(candidate[-1])
    weighted_sum = sum(
        int(digit) * (3 if (len(payload) - 1 - index) % 2 == 0 else 1)
        for index, digit in enumerate(payload)
    )
    expected_check = (10 - weighted_sum % 10) % 10
    return candidate if supplied_check == expected_check else None
