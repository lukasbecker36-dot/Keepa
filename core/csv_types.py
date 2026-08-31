"""Keepa `csv[]` type table.

Transcribed verbatim from the CsvType enum in keepacom/api_backend
(`structs/Product.java`), whose constructor is:

    CsvType(int index, boolean isPrice, boolean isDealRelevant,
            boolean isWithShipping, boolean isExtraData)

`is_with_shipping` is the one that matters structurally: those series are encoded
as [time, price, shipping] TRIPLETS, everything else as [time, value] PAIRS.
BUY_BOX_SHIPPING (18) is a triplet type and is central to Strategy 2 -- decoding
it with a stride of 2 reads shipping costs as timestamps and yields plausible,
entirely wrong numbers. Always take the stride from this table.

Values are in the locale's minor unit (pence for domain 2). A value of -1 means
"no offer at this time" -- for csv[0] that is precisely Amazon being out of stock,
which is the signal Strategy 2 is built on.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CsvType:
    index: int
    name: str
    is_price: bool
    is_deal_relevant: bool
    is_with_shipping: bool
    is_extra_data: bool

    @property
    def stride(self) -> int:
        return 3 if self.is_with_shipping else 2


_RAW: tuple[tuple[str, int, bool, bool, bool, bool], ...] = (
    ("AMAZON", 0, True, True, False, False),
    ("NEW", 1, True, True, False, False),
    ("USED", 2, True, True, False, False),
    ("SALES", 3, False, True, False, False),
    ("LISTPRICE", 4, True, False, False, False),
    ("COLLECTIBLE", 5, True, True, False, False),
    ("REFURBISHED", 6, True, True, False, False),
    ("NEW_FBM_SHIPPING", 7, True, True, True, True),
    ("LIGHTNING_DEAL", 8, True, True, False, False),
    ("WAREHOUSE", 9, True, True, False, True),
    ("NEW_FBA", 10, True, True, False, True),
    ("COUNT_NEW", 11, False, False, False, False),
    ("COUNT_USED", 12, False, False, False, False),
    ("COUNT_REFURBISHED", 13, False, False, False, False),
    ("COUNT_COLLECTIBLE", 14, False, False, False, False),
    ("EXTRA_INFO_UPDATES", 15, False, False, False, True),
    ("RATING", 16, False, False, False, True),
    ("COUNT_REVIEWS", 17, False, False, False, True),
    ("BUY_BOX_SHIPPING", 18, True, False, True, True),
    ("USED_NEW_SHIPPING", 19, True, True, True, True),
    ("USED_VERY_GOOD_SHIPPING", 20, True, True, True, True),
    ("USED_GOOD_SHIPPING", 21, True, True, True, True),
    ("USED_ACCEPTABLE_SHIPPING", 22, True, True, True, True),
    ("COLLECTIBLE_NEW_SHIPPING", 23, True, True, True, True),
    ("COLLECTIBLE_VERY_GOOD_SHIPPING", 24, True, True, True, True),
    ("COLLECTIBLE_GOOD_SHIPPING", 25, True, True, True, True),
    ("COLLECTIBLE_ACCEPTABLE_SHIPPING", 26, True, True, True, True),
    ("REFURBISHED_SHIPPING", 27, True, True, True, True),
    ("EBAY_NEW_SHIPPING", 28, True, False, True, False),
    ("EBAY_USED_SHIPPING", 29, True, False, True, False),
    ("TRADE_IN", 30, True, False, False, False),
    ("RENT", 31, True, False, False, True),
    ("BUY_BOX_USED_SHIPPING", 32, True, True, True, True),
    ("PRIME_EXCL", 33, True, True, False, True),
    ("COUNT_NEW_FBA", 34, False, False, False, False),
    ("COUNT_NEW_FBM", 35, False, False, False, False),
)

BY_INDEX: dict[int, CsvType] = {
    idx: CsvType(idx, name, price, deal, shipping, extra)
    for name, idx, price, deal, shipping, extra in _RAW
}
BY_NAME: dict[str, CsvType] = {t.name: t for t in BY_INDEX.values()}

# Named indices for the series this project actually reads. Use these rather
# than integer literals at call sites.
AMAZON = 0
NEW = 1
SALES = 3
NEW_FBA = 10
COUNT_NEW = 11
RATING = 16
COUNT_REVIEWS = 17
BUY_BOX_SHIPPING = 18
COUNT_NEW_FBA = 34
COUNT_NEW_FBM = 35

# Sentinel used by Keepa for "no offer / not available at this time".
NO_VALUE = -1


def stride_for(csv_index: int) -> int:
    return BY_INDEX[csv_index].stride


def name_for(csv_index: int) -> str:
    t = BY_INDEX.get(csv_index)
    return t.name if t else f"UNKNOWN_{csv_index}"
