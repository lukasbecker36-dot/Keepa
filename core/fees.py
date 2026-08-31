"""The single source of truth for cost and fee arithmetic. All integers in pence.

No strategy computes a fee itself, and nothing here returns a float that
represents money. Third-party "profit" figures are never trusted -- everything is
recomputed from this module (CLAUDE.md, "Data reliability notes").

Operator profile, which drives every default below:
  - NOT VAT registered (under the GBP 90k threshold)
  - Merchant-fulfilled, Royal Mail 2nd class
  - Amazon UK, domain 2

The non-registered position is asymmetric and it is easy to get backwards:

  * Amazon charges VAT ON TOP of its seller fees, and you cannot reclaim it.
    A headline 15% referral fee genuinely costs 18% of the sale price.
  * Purchases cost their gross shelf price. No input VAT reclaim.
  * BUT no output VAT is owed on sales. The sale price is yours gross.

The third point is the largest of the three and it is the one working in your
favour: it is why thin-margin flips are viable now and stop being viable the day
you register. Set VAT_REGISTERED and every branch here inverts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Iterable

# -- tunable constants ----------------------------------------------------
# Everything the operator might need to change lives here, per CLAUDE.md's
# working style note. Verify the starred ones against current rate cards.

VAT_REGISTERED = False
VAT_RATE = 0.20
VAT_THRESHOLD_P = 9_000_000  # GBP 90,000 rolling turnover

# Applied to Amazon fees while unregistered. This is the 1.20 that turns a 15%
# referral fee into a real 18% cost.
FEE_VAT_MULTIPLIER = 1.0 + VAT_RATE

DEFAULT_REFERRAL_PCT = 15.0          # * verify per category
MIN_REFERRAL_FEE_P = 25             # * Amazon UK minimum referral fee
MEDIA_CLOSING_FEE_P = 75            # GBP 0.75, media categories only

# Amazon's Digital Services Fee is a percentage OF THE REFERRAL FEE in some
# marketplaces. Left at zero deliberately -- turn it on only after confirming it
# applies to this account, rather than baking in a number that may not.
DIGITAL_SERVICES_FEE_PCT = 0.0      # * verify

PACKAGING_P = 30                     # GBP 0.30 per unit

# --- FBA -----------------------------------------------------------------
# Amazon's per-unit pick & pack comes from Keepa's fbaFees.pickAndPackFee, per
# ASIN, so no fee table is invented here. Observed range on live UK data:
# 269-315p. What it costs to get a unit INTO a fulfilment centre is not in any
# API and must be estimated.
FBA_INBOUND_PER_UNIT_P = 30         # * verify against a real consolidated shipment
FBA_STORAGE_PER_UNIT_MONTH_P = 0    # Keepa exposes storageFee; usually absent

# Royal Mail 2nd class bands. Only `small_parcel` at GBP 2.95 comes from the
# project brief; the other two are placeholders to be checked against the
# current rate card before they are relied on. Dimensions are the longest side.
SMALL_PARCEL_P = 295


@dataclass(frozen=True)
class PostageBand:
    name: str
    max_weight_g: int
    max_longest_mm: int
    price_p: int
    verified: bool


ROYAL_MAIL_2ND: tuple[PostageBand, ...] = (
    PostageBand("large_letter", 750, 353, 155, verified=False),   # * verify
    PostageBand("small_parcel", 2000, 450, SMALL_PARCEL_P, verified=True),
    PostageBand("medium_parcel", 2000, 610, 489, verified=False),  # * verify
)

# Merchant-fulfilled physical limits (CLAUDE.md negative filters).
MAX_WEIGHT_G = 2000
MAX_LONGEST_MM = 450


class UnpostableError(ValueError):
    """Raised when an item exceeds what merchant-fulfilled postage can carry."""


# -- configuration --------------------------------------------------------


@dataclass(frozen=True)
class FeeConfig:
    """A complete fee environment. Swap one to model registering for VAT."""

    vat_registered: bool = VAT_REGISTERED
    vat_rate: float = VAT_RATE
    fee_vat_multiplier: float = FEE_VAT_MULTIPLIER
    default_referral_pct: float = DEFAULT_REFERRAL_PCT
    min_referral_fee_p: int = MIN_REFERRAL_FEE_P
    media_closing_fee_p: int = MEDIA_CLOSING_FEE_P
    digital_services_fee_pct: float = DIGITAL_SERVICES_FEE_PCT
    packaging_p: int = PACKAGING_P
    postage_p: int = SMALL_PARCEL_P

    def registered(self) -> "FeeConfig":
        """The same environment after VAT registration. Not the current state --
        provided so the effect can be modelled before crossing the threshold."""
        return replace(self, vat_registered=True, fee_vat_multiplier=1.0)


DEFAULT = FeeConfig()


def _round_p(value: float) -> int:
    """Round half up, to the penny. Python's round() is banker's rounding, which
    would bias fee totals downward across a large shortlist."""
    return int(math.floor(value + 0.5))


# -- inputs from Keepa ----------------------------------------------------


def referral_pct_for(product: dict | None, cfg: FeeConfig = DEFAULT) -> float:
    """Referral percentage for an ASIN.

    Keepa returns `referralFeePercentage` per product, so use it rather than
    assuming a flat rate across categories -- the flat assumption is a needless
    source of error when the real number is right there in the payload.
    """
    if product:
        pct = product.get("referralFeePercentage")
        if isinstance(pct, (int, float)) and pct > 0:
            return float(pct)
    return cfg.default_referral_pct


# Amazon charges a per-item closing fee on media. Keepa's `type` field carries
# the product classification; categoryTree is the fallback.
MEDIA_TYPES = frozenset({
    "ABIS_BOOK", "ABIS_MUSIC", "ABIS_DVD", "ABIS_VIDEO", "BOOK",
    "PHYSICAL_VIDEO_GAME_SOFTWARE", "VIDEO_GAME", "MUSIC", "DVD",
})
MEDIA_CATEGORY_HINTS = (
    "books", "music", "dvd", "blu-ray", "video games", "pc & video games",
    "cds & vinyl", "software",
)


def is_media_product(product: dict | None) -> bool:
    """Whether the media closing fee applies. Checked from Keepa's `type` first.

    Missing this understates cost by 75p plus VAT on every unit -- small, but it
    is exactly the kind of quiet error that makes a shortlist flatter itself.
    """
    if not product:
        return False
    if str(product.get("type") or "").upper() in MEDIA_TYPES:
        return True
    names = " ".join(
        str(e.get("name", "")).lower()
        for e in (product.get("categoryTree") or [])
    )
    return any(hint in names for hint in MEDIA_CATEGORY_HINTS)


def fba_fee_for(product: dict | None) -> int | None:
    """Amazon's pick & pack for this ASIN, in pence, from Keepa. None if absent.

    Note this is an AMAZON fee, so while unregistered it attracts 20% VAT you
    cannot reclaim -- unlike Royal Mail postage, which is VAT-exempt. That
    asymmetry is the whole story when comparing FBA against merchant-fulfilled
    on a non-VAT-registered account.
    """
    fees_obj = (product or {}).get("fbaFees") or {}
    value = fees_obj.get("pickAndPackFee")
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    return None


def postage_for(
    weight_g: int | None,
    longest_mm: int | None = None,
    cfg: FeeConfig = DEFAULT,
    *,
    bands: Iterable[PostageBand] = ROYAL_MAIL_2ND,
) -> int:
    """Cheapest Royal Mail band that fits, in pence.

    Unknown weight falls back to the configured small-parcel price rather than
    guessing cheap: understating postage silently inflates every margin.
    """
    if weight_g is None:
        return cfg.postage_p
    if weight_g > MAX_WEIGHT_G:
        raise UnpostableError(
            f"{weight_g}g exceeds the {MAX_WEIGHT_G}g merchant-fulfilled limit"
        )
    if longest_mm is not None and longest_mm > MAX_LONGEST_MM:
        raise UnpostableError(
            f"{longest_mm}mm exceeds the {MAX_LONGEST_MM}mm limit"
        )
    for band in bands:
        if weight_g > band.max_weight_g:
            continue
        if longest_mm is None:
            # Weight alone does not prove an item fits a cheaper band. A 400g
            # kitchen gadget is under the large-letter weight limit and will
            # not go through a large-letter slot, so without confirmed
            # dimensions never price below the configured default.
            if band.price_p < cfg.postage_p:
                continue
        elif longest_mm > band.max_longest_mm:
            continue
        return band.price_p
    raise UnpostableError(f"no postage band fits {weight_g}g / {longest_mm}mm")


# -- the model ------------------------------------------------------------


@dataclass(frozen=True)
class Breakdown:
    """Every line of a single unit's economics. Sums are exact in pence."""

    sell_price_p: int
    referral_fee_p: int
    closing_fee_p: int
    digital_services_fee_p: int
    fee_vat_p: int
    output_vat_p: int
    postage_p: int
    packaging_p: int
    fba_fee_p: int = 0

    @property
    def total_fees_p(self) -> int:
        return (
            self.referral_fee_p
            + self.closing_fee_p
            + self.digital_services_fee_p
            + self.fba_fee_p
            + self.fee_vat_p
        )

    @property
    def total_costs_p(self) -> int:
        """Everything deducted from the sale price, excluding the goods."""
        return self.total_fees_p + self.output_vat_p + self.postage_p + self.packaging_p

    @property
    def net_p(self) -> int:
        """Proceeds after selling costs, before the cost of the goods."""
        return self.sell_price_p - self.total_costs_p

    def profit_p(self, landed_cost_p: int) -> int:
        return self.net_p - landed_cost_p

    def margin_pct(self, landed_cost_p: int) -> float:
        if self.sell_price_p <= 0:
            return 0.0
        return self.profit_p(landed_cost_p) / self.sell_price_p * 100

    def roi_pct(self, landed_cost_p: int) -> float:
        """Return on the capital actually committed. For a small float this is
        the number that matters more than margin -- it sets how fast money
        recycles."""
        if landed_cost_p <= 0:
            return 0.0
        return self.profit_p(landed_cost_p) / landed_cost_p * 100

    def explain(self) -> str:
        def gbp(p: int) -> str:
            return f"£{p / 100:,.2f}"

        lines = [f"sell            {gbp(self.sell_price_p):>10}"]
        lines.append(f"referral fee    {gbp(-self.referral_fee_p):>10}")
        if self.closing_fee_p:
            lines.append(f"closing fee     {gbp(-self.closing_fee_p):>10}")
        if self.digital_services_fee_p:
            lines.append(f"digital svcs    {gbp(-self.digital_services_fee_p):>10}")
        if self.fee_vat_p:
            lines.append(f"VAT on fees     {gbp(-self.fee_vat_p):>10}  (unreclaimable)")
        if self.output_vat_p:
            lines.append(f"output VAT      {gbp(-self.output_vat_p):>10}")
        if self.fba_fee_p:
            lines.append(f"FBA pick&pack   {gbp(-self.fba_fee_p):>10}")
        if self.postage_p:
            lines.append(f"postage         {gbp(-self.postage_p):>10}")
        if self.packaging_p:
            lines.append(f"packaging       {gbp(-self.packaging_p):>10}")
        lines.append(f"net proceeds    {gbp(self.net_p):>10}")
        return "\n".join(lines)


def net_proceeds(
    sell_price_p: int,
    *,
    referral_pct: float | None = None,
    is_media: bool = False,
    postage_p: int | None = None,
    packaging_p: int | None = None,
    fba_fee_p: int = 0,
    cfg: FeeConfig = DEFAULT,
) -> Breakdown:
    """What actually lands in the account from one sale.

    Merchant-fulfilled: pass postage_p and packaging_p, leave fba_fee_p at 0.
    FBA: pass fba_fee_p, and postage_p=0, packaging_p=0 -- Amazon ships it.

    The distinction that matters while unregistered: fba_fee_p is an AMAZON fee
    and is grossed by fee VAT; postage_p is Royal Mail and is VAT-exempt.
    """
    if sell_price_p < 0:
        raise ValueError("sell price cannot be negative")

    pct = cfg.default_referral_pct if referral_pct is None else referral_pct
    referral = max(_round_p(sell_price_p * pct / 100.0), cfg.min_referral_fee_p)
    closing = cfg.media_closing_fee_p if is_media else 0
    dsf = _round_p(referral * cfg.digital_services_fee_pct / 100.0)
    amazon_fees = referral + closing + dsf + fba_fee_p

    if cfg.vat_registered:
        # Fee VAT is reclaimable, so it is not a cost. Output VAT is owed on the
        # sale: the price is VAT-inclusive, so the tax is 1/6 of it at 20%.
        fee_vat = 0
        output_vat = _round_p(
            sell_price_p * cfg.vat_rate / (1.0 + cfg.vat_rate)
        )
    else:
        # Fee VAT is a real, unrecoverable cost. No output VAT is owed.
        fee_vat = _round_p(amazon_fees * (cfg.fee_vat_multiplier - 1.0))
        output_vat = 0

    return Breakdown(
        sell_price_p=sell_price_p,
        referral_fee_p=referral,
        closing_fee_p=closing,
        digital_services_fee_p=dsf,
        fee_vat_p=fee_vat,
        output_vat_p=output_vat,
        postage_p=cfg.postage_p if postage_p is None else postage_p,
        packaging_p=cfg.packaging_p if packaging_p is None else packaging_p,
        fba_fee_p=fba_fee_p,
    )


def landed_cost(
    unit_cost_p: int, inbound_shipping_p: int = 0, *, cfg: FeeConfig = DEFAULT
) -> int:
    """What a unit really costs to have in hand.

    While unregistered this is simply what you paid -- the gross shelf price,
    with no VAT to reclaim. Once registered, input VAT on a VAT-bearing purchase
    comes back, so the same shelf price costs 1/1.2 of it.
    """
    gross = unit_cost_p + inbound_shipping_p
    if cfg.vat_registered:
        return _round_p(gross / (1.0 + cfg.vat_rate))
    return gross


def profit(
    sell_price_p: int,
    unit_cost_p: int,
    *,
    inbound_shipping_p: int = 0,
    referral_pct: float | None = None,
    is_media: bool = False,
    postage_p: int | None = None,
    packaging_p: int | None = None,
    fba_fee_p: int = 0,
    cfg: FeeConfig = DEFAULT,
) -> int:
    """Net profit per unit, in pence. Negative is a loss."""
    breakdown = net_proceeds(
        sell_price_p,
        referral_pct=referral_pct,
        is_media=is_media,
        postage_p=postage_p,
        packaging_p=packaging_p,
        fba_fee_p=fba_fee_p,
        cfg=cfg,
    )
    return breakdown.profit_p(landed_cost(unit_cost_p, inbound_shipping_p, cfg=cfg))


# -- price-scaled thresholds ---------------------------------------------
#
# Postage and packaging are FLAT (GBP 3.25 on every unit) while fees are
# proportional. So the uplift a flip needs is a function of price, not a
# constant: cheap items must rise much further to clear the same fixed costs.
#
# Measured with the default config and a 15% referral:
#
#     cost    breakeven uplift    uplift for GBP 3/unit
#     £15          48%                    85%
#     £22          40%                    57%
#     £30          35%                    47%
#     £50          30%                    37%
#     £60          29%                    35%
#
# A flat 40% rule therefore LOSES MONEY below about £22 and is slack above £45.
# Strategy 2 uses required_uplift() instead of a constant.

MIN_PROFIT_PER_UNIT_P = 300  # GBP 3.00 -- below this the handling is not worth it


def required_sell_price(
    unit_cost_p: int,
    target_profit_p: int = 0,
    *,
    inbound_shipping_p: int = 0,
    referral_pct: float | None = None,
    is_media: bool = False,
    postage_p: int | None = None,
    packaging_p: int | None = None,
    fba_fee_p: int = 0,
    cfg: FeeConfig = DEFAULT,
) -> int:
    """Lowest sale price yielding at least `target_profit_p` per unit.

    Solved analytically, then nudged against the real `profit()` to absorb
    rounding, the minimum referral fee and the media closing fee -- so the answer
    is exact under the same function the strategies use, not merely close.
    """
    if unit_cost_p < 0:
        raise ValueError("unit cost cannot be negative")

    pct = cfg.default_referral_pct if referral_pct is None else referral_pct
    post = cfg.postage_p if postage_p is None else postage_p
    pack = cfg.packaging_p if packaging_p is None else packaging_p
    landed = landed_cost(unit_cost_p, inbound_shipping_p, cfg=cfg)

    if cfg.vat_registered:
        rate = pct / 100.0 + cfg.vat_rate / (1.0 + cfg.vat_rate)
        fixed = (cfg.media_closing_fee_p if is_media else 0) + fba_fee_p
    else:
        rate = pct / 100.0 * cfg.fee_vat_multiplier
        # The FBA fee is an Amazon fee, so it carries unreclaimable VAT here.
        # Royal Mail postage does not. That asymmetry is why FBA can cost MORE
        # than merchant-fulfilled on a non-VAT-registered account.
        fixed = _round_p(
            ((cfg.media_closing_fee_p if is_media else 0) + fba_fee_p)
            * cfg.fee_vat_multiplier
        )
    fixed += post + pack

    if rate >= 1.0:
        raise ValueError(f"referral rate of {pct}% leaves nothing to sell into")

    guess = max(int((landed + fixed + target_profit_p) / (1.0 - rate)), 1)

    def meets(price: int) -> bool:
        return (
            profit(
                price,
                unit_cost_p,
                inbound_shipping_p=inbound_shipping_p,
                referral_pct=pct,
                is_media=is_media,
                postage_p=post,
                packaging_p=pack,
                fba_fee_p=fba_fee_p,
                cfg=cfg,
            )
            >= target_profit_p
        )

    # The analytic guess is within a few pence; walk to the exact boundary.
    if meets(guess):
        while guess > 1 and meets(guess - 1):
            guess -= 1
    else:
        while not meets(guess):
            guess += 1
            if guess > 100_000_000:  # unreachable in practice; avoids a hang
                raise ValueError("no sale price reaches the target profit")
    return guess


def breakeven_sell_price(unit_cost_p: int, **kw) -> int:
    """Sale price at which the unit exactly washes its face."""
    return required_sell_price(unit_cost_p, 0, **kw)


def required_uplift(
    unit_cost_p: int, target_profit_p: int = MIN_PROFIT_PER_UNIT_P, **kw
) -> float:
    """Multiplier the sale price must reach, as a ratio of unit cost.

    This replaces Strategy 2's flat 1.40 gap-delta threshold. Returns e.g. 1.57
    meaning "the gap price must be at least 57% above what Amazon charges".
    """
    if unit_cost_p <= 0:
        raise ValueError("unit cost must be positive to express an uplift")
    return required_sell_price(unit_cost_p, target_profit_p, **kw) / unit_cost_p


def max_viable_cost(
    sell_price_p: int,
    target_profit_p: int = MIN_PROFIT_PER_UNIT_P,
    **kw,
) -> int:
    """Highest landed unit cost that still clears `target_profit_p`.

    The inverse of the usual question, and the one that matters for private
    label: the sale price is set by the incumbent listing, and what you control
    is what you pay a supplier. "Source below GBP X/unit or do not bother" is an
    actionable instruction; a score is not.

    Returns pence. Negative means the sale price cannot support the target at
    all, whatever the goods cost.
    """
    return net_proceeds(sell_price_p, **kw).net_p - target_profit_p


# -- importing ------------------------------------------------------------
# Import VAT is charged at 20% on (goods + freight + duty) at the UK border.
# While NOT VAT registered it is UNRECLAIMABLE, so it is a straight 20% tax on
# the whole cost base -- a cost a VAT-registered competitor does not carry.
#
# This inverts the usual advice. For Strategy 2 (buying from Amazon UK and
# reselling) being unregistered HELPS: no output VAT on the sale. For Strategy 1
# (importing) it HURTS, badly, because every unit costs 20% more to land.

DEFAULT_DUTY_PCT = 3.7        # * verify the commodity code; many parts are 0%
DEFAULT_FREIGHT_PER_UNIT_P = 200   # * a placeholder until a real quote exists


def import_landed_cost(
    fob_price_p: int,
    *,
    freight_per_unit_p: int = DEFAULT_FREIGHT_PER_UNIT_P,
    duty_pct: float = DEFAULT_DUTY_PCT,
    cfg: FeeConfig = DEFAULT,
) -> int:
    """Cost of one imported unit delivered into your hands, in pence.

    landed = (FOB + freight) x (1 + duty) x (1 + VAT if unreclaimable)
    """
    cif = fob_price_p + freight_per_unit_p
    duty_paid = cif * (1.0 + duty_pct / 100.0)
    if cfg.vat_registered:
        return _round_p(duty_paid)          # import VAT reclaimed
    return _round_p(duty_paid * (1.0 + cfg.vat_rate))


def max_fob_price(
    max_landed_p: int,
    *,
    freight_per_unit_p: int = DEFAULT_FREIGHT_PER_UNIT_P,
    duty_pct: float = DEFAULT_DUTY_PCT,
    cfg: FeeConfig = DEFAULT,
) -> int:
    """Highest price a supplier may quote, given a landed-cost ceiling.

    This is the number to take to Alibaba. `max_viable_cost` gives the LANDED
    ceiling; a quoted FOB price has freight, duty and unreclaimable import VAT
    still to be added on top of it.
    """
    divisor = 1.0 + duty_pct / 100.0
    if not cfg.vat_registered:
        divisor *= 1.0 + cfg.vat_rate
    return _round_p(max_landed_p / divisor) - freight_per_unit_p


def uplift_table(
    costs_p: Iterable[int],
    target_profit_p: int = MIN_PROFIT_PER_UNIT_P,
    **kw,
) -> list[tuple[int, int, float, float]]:
    """(cost, breakeven price, breakeven uplift, uplift for target profit).

    Used to sanity-check thresholds and to print the justification into a run's
    summary.md, so a shortlist carries its own reasoning.
    """
    rows = []
    for cost in costs_p:
        be = breakeven_sell_price(cost, **kw)
        rows.append(
            (
                cost,
                be,
                be / cost,
                required_uplift(cost, target_profit_p, **kw),
            )
        )
    return rows
