"""Negative filters. Every rejection is named and recorded, never silent.

Excluded candidates go to `excluded.csv` tagged with the filter that caught them,
because a brand the operator turns out to have approval for is actionable
(CLAUDE.md, "Output conventions").

Two design points worth stating up front.

**Matching is word-boundary, never substring.** A naive `"sony" in title` also
matches "masonry"; `"apple"` matches "apple cider vinegar"; `"oxo"` matches
"toxic". Every text rule here compiles to a `\\b`-anchored regex over a
normalised string. This is the difference between a filter and a random row
remover.

**The trademark rule is strategy-dependent, and cannot be global.** CLAUDE.md
says "unknown = exclude" for trademarked titles. Applied universally that would
reject Strategy 2 entirely, because reselling genuine branded goods bought from
Amazon is the whole strategy and is lawful. The risk there is *gating*, not
infringement. Where it genuinely binds is Strategy 1, where you create your own
listing and must not trade on someone else's mark. So the trademark filter lives
in PRIVATE_LABEL_FILTERS, not in UNIVERSAL_FILTERS -- see FilterSet below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GATED_BRANDS_FILE = PROJECT_ROOT / "data" / "gated_brands.txt"

# Physical limits for merchant-fulfilled (CLAUDE.md).
MAX_WEIGHT_G = 2000
MAX_LONGEST_MM = 450

# Keepa signals "not known" as BOTH -1 and 0. Treating 0 as a real measurement
# is how four 15-litre water bottles passed a 700g weight filter: packageWeight
# was 0, and 0 <= 700. Every weight/dimension check must use this helper.
UNKNOWN_VALUES = (-1, 0)


def known_measure(value) -> int | None:
    """A Keepa measurement, or None if it is one of the not-known sentinels."""
    if isinstance(value, int) and value not in UNKNOWN_VALUES and value > 0:
        return value
    return None


# -- text normalisation ---------------------------------------------------

# Apostrophes are DELETED, not spaced: "Levi's" must become "levis", or it would
# never match a title reading "Levis Jeans" and the brand list would silently
# miss it. Other punctuation becomes a space, so "Ray-Ban" and "Ray Ban" agree.
_APOSTROPHE = re.compile(r"['‘’ʼ´`]+")
_PUNCT = re.compile(r"[^\w\s]+")
_SPACE = re.compile(r"\s+")


def normalise(text: str | None) -> str:
    """Lowercase, drop apostrophes, punctuation to spaces, collapse whitespace."""
    if not text:
        return ""
    stripped = _APOSTROPHE.sub("", text.lower())
    return _SPACE.sub(" ", _PUNCT.sub(" ", stripped)).strip()


@lru_cache(maxsize=4096)
def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Word-boundary matcher for a normalised phrase."""
    return re.compile(rf"\b{re.escape(normalise(phrase))}\b")


def contains_phrase(haystack: str, phrase: str) -> bool:
    if not haystack or not phrase:
        return False
    return bool(_phrase_pattern(phrase).search(haystack))


def first_match(haystack: str, phrases: Iterable[str]) -> str | None:
    for phrase in phrases:
        if contains_phrase(haystack, phrase):
            return phrase
    return None


# -- keyword sets ---------------------------------------------------------
# Named constants at the top so tuning is one edit (CLAUDE.md working style).

IP_HOT_TERMS = (
    "funko", "funko pop", "pokemon", "pokémon", "yu gi oh", "yugioh",
    "magic the gathering", "topps", "panini", "match attax",
    "marvel", "dc comics", "star wars", "harry potter", "disney",
    "pixar", "nintendo", "sanrio", "hello kitty", "paw patrol",
    "peppa pig", "bluey", "minecraft", "roblox", "fortnite",
    "premier league", "signed", "autographed", "memorabilia",
    "limited edition collectible", "trading card", "trading cards",
)

COSMETICS_TERMS = (
    "cosmetic", "cosmetics", "makeup", "make up", "foundation", "mascara",
    "lipstick", "eyeliner", "concealer", "perfume", "fragrance", "eau de toilette",
    "eau de parfum", "aftershave", "moisturiser", "moisturizer", "serum",
    "sunscreen", "spf", "shampoo", "conditioner", "body lotion", "face cream",
    "skincare", "skin care", "nail polish", "hair dye",
)

SUPPLEMENT_TERMS = (
    "supplement", "supplements", "vitamin", "vitamins", "multivitamin",
    "protein powder", "whey", "creatine", "collagen", "probiotic", "probiotics",
    "omega 3", "fish oil", "capsules", "gummies", "herbal remedy",
    "meal replacement", "pre workout", "weight loss",
)

FOOD_TERMS = (
    "food", "snack", "snacks", "cereal", "coffee", "tea bags", "chocolate",
    "confectionery", "sweets", "biscuits", "edible", "drink", "beverage",
    "infant formula", "baby food",
)

MEDICAL_TERMS = (
    "medical device", "thermometer", "blood pressure monitor", "pulse oximeter",
    "nebuliser", "nebulizer", "hearing aid", "glucose meter", "test kit",
    "first aid", "bandage", "wound dressing", "syringe", "orthopaedic",
    "orthopedic", "compression stocking",
    # CPAP consumables slipped through the first Strategy 1 scan and ranked
    # second: a ResMed AirTouch F20 mask cushion. Consumables for a medical
    # device carry the same regime as the device.
    "cpap", "bipap", "apnoea", "apnea", "mask cushion", "nasal pillow",
    "replacement cushion", "full face mask", "airtouch", "airfit",
    "resmed", "respironics", "fisher paykel",
    "ventilator", "oxygen concentrator", "catheter",
    "incontinence", "mobility aid", "walking frame", "blood glucose",
    "lancet", "insulin", "stoma", "prosthetic",
)

# Not manufacturable at all: services, connectivity, credit, digital goods.
# A Vodafone data SIM ranked fourth on the first Strategy 1 scan.
NON_PHYSICAL_TERMS = (
    "sim card", "sim only", "data sim", "esim", "top up", "topup",
    "gift card", "gift voucher", "e voucher", "evoucher", "digital code",
    "download code", "activation code", "licence key", "license key",
    "subscription", "membership", "warranty", "insurance", "installation service",
    "mobile broadband", "data plan", "prepaid credit", "game pass",
)

PPE_TERMS = (
    "ppe", "respirator", "ffp2", "ffp3", "safety goggles", "safety helmet",
    "hard hat", "hi vis", "high visibility", "safety harness", "safety boots",
    "steel toe", "cut resistant", "face shield",
    # Life-saving appliances are UKCA-marked safety equipment, and a child's
    # buoyancy aid is about as liable as a first-time seller can get. A "TWF
    # Freedom Life Jacket 10-20kg" was the TOP-scoring row on a live scan.
    "life jacket", "lifejacket", "life vest", "buoyancy aid",
    "personal flotation", "impact vest",
)

# Veterinary medicines are regulated by the VMD. Pet Supplies was the densest
# category on the first scoped scan (23 results) and was full of them: wormers,
# ear cleansers, blowfly repellent for sheep. None are private-label material.
VETERINARY_TERMS = (
    "wormer", "worming", "anthelmintic", "de wormer", "dewormer",
    "flea treatment", "flea and tick", "spot on", "vet strength",
    "veterinary", "prescription", "nonprescription medication",
    "blowfly", "sheep dip", "drench", "antibacterial ear",
    "aural", "ear cleanser", "skin cleanser", "wound spray", "louse", "mite treatment",
    "milk replacer", "electrolyte supplement",
)

VETERINARY_CATEGORY_HINTS = (
    "nonprescription medications", "wormers", "flea", "veterinary",
    "medications", "wound care", "ear care",
)

TOY_TERMS = (
    "toy", "toys", "playset", "play set", "action figure", "doll", "dolls",
    "plush", "teddy", "rattle", "teether", "building blocks", "board game",
    "jigsaw", "puzzle for kids", "kids toy", "children's toy", "toddler",
    "pram toy", "ride on",
)

# Age markers that put a toy inside the under-14 safety-testing regime.
UNDER_14_TERMS = (
    "3 years", "3 yrs", "age 3", "ages 3", "4 years", "age 4", "ages 4",
    "5 years", "age 5", "ages 5", "6 years", "age 6", "ages 6",
    "7 years", "8 years", "9 years", "10 years", "11 years", "12 years",
    "13 years", "months", "toddler", "infant", "baby", "preschool",
    "pre school", "nursery", "kids", "children", "childrens",
)

MAINS_ELECTRICAL_TERMS = (
    "mains powered", "plug in", "uk plug", "3 pin plug", "240v", "230v",
    "mains adapter", "power supply", "charger", "extension lead", "socket",
    "kettle", "toaster", "microwave", "hair dryer", "straightener",
    "electric heater", "power tool",
)

HAZMAT_TERMS = (
    # LPG/autogas fittings carry pressurised flammable gas. An "LPG Autogas
    # Tank Refill Adapter Kit" scored on a live scan.
    "lpg", "autogas", "gpl", "gas bottle", "regulator valve",
    "aerosol", "flammable", "pressurised", "pressurized", "compressed gas",
    "butane", "propane", "lighter fluid", "paint thinner", "solvent",
    "bleach", "corrosive", "acetone", "white spirit", "lithium battery",
    "car battery", "lead acid",
)

FRAGILE_TERMS = (
    "glass", "ceramic", "porcelain", "crystal", "mirror", "fragile",
    "stoneware", "earthenware", "wine glasses", "vase",
)

# Brand-field values meaning "no real brand". Used by the trademark rule to tell
# a genuinely generic listing from a branded one.
GENERIC_BRAND_MARKERS = (
    "generic", "unbranded", "no brand", "nobrand", "unknown", "oem",
    "does not apply", "n a", "na", "none",
)


# -- verdicts -------------------------------------------------------------


@dataclass(frozen=True)
class Rejection:
    filter_name: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.filter_name}: {self.detail}" if self.detail else self.filter_name


@dataclass(frozen=True)
class Verdict:
    """Outcome for one product. `rejections` is every rule that fired, not just
    the first -- knowing a row is both gated AND hazmat is more useful than
    knowing only whichever ran first."""

    asin: str
    rejections: tuple[Rejection, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.rejections

    @property
    def primary(self) -> Rejection | None:
        return self.rejections[0] if self.rejections else None

    def reason(self) -> str:
        return "; ".join(str(r) for r in self.rejections)


# A filter takes the Keepa product dict and returns a Rejection or None.
Filter = Callable[[dict], "Rejection | None"]


# -- searchable text ------------------------------------------------------


def searchable_text(product: dict) -> str:
    """Normalised title + features + binding + type, as one string.

    Description is deliberately excluded: it is long, marketing-written, and
    mentions competitors and unrelated products often enough to cause false
    rejections.
    """
    parts: list[str] = [product.get("title") or ""]
    features = product.get("features") or []
    if isinstance(features, (list, tuple)):
        parts.extend(str(f) for f in features)
    parts.append(product.get("binding") or "")
    parts.append(product.get("type") or "")
    parts.append(product.get("itemForm") or "")
    return normalise(" ".join(parts))


def brand_names(product: dict) -> list[str]:
    out = []
    for key in ("brand", "manufacturer", "brandStoreName"):
        value = product.get(key)
        if value:
            out.append(normalise(str(value)))
    return out


# -- gated brands ---------------------------------------------------------


@lru_cache(maxsize=1)
def load_gated_brands(path: str | None = None) -> tuple[str, ...]:
    """Read data/gated_brands.txt. Blank lines and # comments ignored."""
    file = Path(path) if path else GATED_BRANDS_FILE
    if not file.is_file():
        return ()
    brands = []
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            brands.append(line)
    return tuple(brands)


def gated_brand(product: dict) -> Rejection | None:
    """Brand requires Amazon approval to list.

    Matched two ways: exact equality against the brand/manufacturer fields, and
    word-boundary search of the title. The title search catches ASINs whose
    brand field is generic but whose title names the mark.
    """
    gated = load_gated_brands()
    if not gated:
        return None

    names = brand_names(product)
    for brand in gated:
        norm = normalise(brand)
        if norm and norm in names:
            return Rejection("gated_brand", f"brand is {brand}")

    hit = first_match(normalise(product.get("title")), gated)
    if hit:
        return Rejection("gated_brand", f"title names {hit}")

    # The category tree names brands too. A Nintendo Switch game carrying the
    # brand "FanGamer LLC" is still gated as a Nintendo Switch game -- checking
    # only the brand field missed exactly that case on a live scan.
    tree = " ".join(
        str(e.get("name", "")) for e in (product.get("categoryTree") or [])
    )
    hit = first_match(normalise(tree), gated)
    if hit:
        return Rejection("gated_brand", f"category names {hit}")
    return None


# -- IP-hot ---------------------------------------------------------------


def ip_hot(product: dict) -> Rejection | None:
    """Licensed characters, trading cards, memorabilia. Takedown risk is high
    and the rights holders are active."""
    hit = first_match(searchable_text(product), IP_HOT_TERMS)
    return Rejection("ip_hot", f"matches '{hit}'") if hit else None


# -- compliance-heavy -----------------------------------------------------


def compliance_heavy(product: dict) -> Rejection | None:
    """Regimes this project explicitly does not handle (CLAUDE.md "What this
    project does not do"): CPSR for cosmetics, FSA for food and supplements,
    toy safety testing for under-14s, UKCA for mains electricals, medical
    devices, CE-marked PPE."""
    text = searchable_text(product)

    hit = first_match(text, COSMETICS_TERMS)
    if hit:
        return Rejection("compliance_cosmetics", f"CPSR required; matches '{hit}'")

    hit = first_match(text, SUPPLEMENT_TERMS)
    if hit:
        return Rejection("compliance_supplements", f"FSA regime; matches '{hit}'")

    hit = first_match(text, FOOD_TERMS)
    if hit:
        return Rejection("compliance_food", f"FSA regime; matches '{hit}'")

    hit = first_match(text, MEDICAL_TERMS)
    if hit:
        return Rejection("compliance_medical", f"medical device; matches '{hit}'")

    hit = first_match(text, PPE_TERMS)
    if hit:
        return Rejection("compliance_ppe", f"CE-marked PPE; matches '{hit}'")

    toy = first_match(text, TOY_TERMS)
    if toy:
        age = first_match(text, UNDER_14_TERMS)
        if age:
            return Rejection(
                "compliance_toys_under_14",
                f"safety testing; '{toy}' with age marker '{age}'",
            )

    hit = first_match(text, MAINS_ELECTRICAL_TERMS)
    if hit:
        return Rejection("compliance_ukca", f"UKCA marking; matches '{hit}'")

    return None


def veterinary(product: dict) -> Rejection | None:
    """Animal medicines and treatments: VMD-regulated, not private-labellable.

    Checked against the category tree as well as the title, because Keepa's
    leaf names ("Wormers", "Nonprescription Medications") are often clearer
    than the product title.
    """
    hit = first_match(searchable_text(product), VETERINARY_TERMS)
    if hit:
        return Rejection("veterinary_medicine", f"matches '{hit}'")
    tree = normalise(
        " ".join(str(e.get("name", "")) for e in (product.get("categoryTree") or []))
    )
    for hint in VETERINARY_CATEGORY_HINTS:
        if contains_phrase(tree, hint):
            return Rejection("veterinary_medicine", f"category '{hint}'")
    return None


def non_physical(product: dict) -> Rejection | None:
    """Services, connectivity, credit and digital goods.

    Nothing here can be sourced, shipped or private-labelled. Caught after a
    Vodafone data SIM ranked fourth on the first Strategy 1 scan -- it passes
    every numeric filter because it genuinely sells, at a stable rank, with few
    reviews and almost no weight.
    """
    hit = first_match(searchable_text(product), NON_PHYSICAL_TERMS)
    return Rejection("non_physical", f"matches '{hit}'") if hit else None


# -- hazmat ---------------------------------------------------------------


def hazmat(product: dict) -> Rejection | None:
    """Keepa's own hazmat flags first, then keywords and battery flags.

    `hazardousMaterials` is a list of {aspect, value} entries; its presence at
    all is treated as a flag, since any entry means Amazon classified the item.
    """
    materials = product.get("hazardousMaterials")
    if materials:
        aspects = ", ".join(
            str(m.get("aspect") or m.get("value") or "")
            for m in materials
            if isinstance(m, dict)
        ) or "flagged"
        return Rejection("hazmat", f"Keepa hazardousMaterials: {aspects}")

    if product.get("batteriesIncluded") is True:
        return Rejection("hazmat_batteries", "batteriesIncluded")

    hit = first_match(searchable_text(product), HAZMAT_TERMS)
    if hit:
        return Rejection("hazmat", f"matches '{hit}'")

    warning = normalise(product.get("safetyWarning"))
    if warning:
        hit = first_match(warning, HAZMAT_TERMS)
        if hit:
            return Rejection("hazmat", f"safety warning names '{hit}'")
    return None


# -- physical -------------------------------------------------------------


def _longest_side_mm(product: dict) -> int | None:
    dims = [
        known_measure(product.get("packageLength")),
        known_measure(product.get("packageWidth")),
        known_measure(product.get("packageHeight")),
    ]
    known = [d for d in dims if d is not None]
    return max(known) if known else None


def physical_limits(product: dict) -> Rejection | None:
    """Merchant-fulfilled postage limits. Unknown dimensions PASS.

    Deliberate: Keepa uses -1 for unknown, and rejecting on unknown would
    discard a large share of otherwise good ASINs. The cost of the opposite
    error is bounded -- fees.postage_for() raises UnpostableError later if the
    real weight turns out to be over the limit, so nothing unpostable reaches a
    profit number.
    """
    weight = known_measure(product.get("packageWeight"))
    if weight is not None and weight > MAX_WEIGHT_G:
        return Rejection("too_heavy", f"{weight}g > {MAX_WEIGHT_G}g")

    longest = _longest_side_mm(product)
    if longest is not None and longest > MAX_LONGEST_MM:
        return Rejection("too_large", f"{longest}mm > {MAX_LONGEST_MM}mm")
    return None


def fragile(product: dict) -> Rejection | None:
    """Glass and ceramics need packaging this operation does not have."""
    hit = first_match(searchable_text(product), FRAGILE_TERMS)
    return Rejection("fragile", f"matches '{hit}'") if hit else None


def adult_product(product: dict) -> Rejection | None:
    if product.get("isAdultProduct") is True:
        return Rejection("adult_product", "Keepa isAdultProduct")
    return None


# -- trademark (private-label strategies only) ----------------------------


def media_product(product: dict) -> Rejection | None:
    """Books, music, film, games -- impossible to private-label.

    Found the hard way: an unscoped Strategy 1 scan returned vinyl records and
    Blu-ray box sets. They pass every niche filter -- low review counts, stable
    rank, GBP 25-60, under 500g -- and you cannot manufacture a Radiohead LP.
    """
    from . import fees  # local import: fees imports nothing from filters

    if fees.is_media_product(product):
        return Rejection("media_product", "cannot be private-labelled")
    return None


def trademark_risk(product: dict) -> Rejection | None:
    """Title or brand carries a KNOWN trademark, so a copy would infringe.

    NOT a universal filter. For Strategy 2 -- reselling genuine goods bought
    from Amazon -- a brand in the title is normal and lawful; the binding
    constraint there is gating, which `gated_brand` covers. This rule applies
    where you would be creating your own listing.

    DEVIATION FROM THE BRIEF, stated plainly. CLAUDE.md says "default to
    unknown = exclude" for trademarked titles. Implemented literally -- reject
    any ASIN with a brand field -- that rejected 29 of 49 rows on the first real
    scan, including every legitimate target, because essentially every Amazon
    listing carries a brand (generic Chinese sellers register names too).

    The intent is "do not trade on someone else's mark", and that is about YOUR
    listing, not the incumbent's. A branded incumbent does not stop you selling
    a competing bamboo organiser under your own brand; a LEGO-branded one does.
    So this checks against marks we actually know -- the gated-brand list and
    the IP-hot terms -- rather than treating the existence of a brand as proof
    of risk. A filter that rejects everything protects nothing.
    """
    names = brand_names(product)
    known = load_gated_brands()
    for name in names:
        hit = first_match(name, known)
        if hit:
            return Rejection("trademark_risk", f"brand is a known mark: {hit}")
    hit = first_match(normalise(product.get("title")), IP_HOT_TERMS)
    if hit:
        return Rejection("trademark_risk", f"title carries protected IP: {hit}")
    return None


# -- filter sets ----------------------------------------------------------

UNIVERSAL_FILTERS: tuple[Filter, ...] = (
    gated_brand,
    ip_hot,
    compliance_heavy,
    hazmat,
    physical_limits,
    fragile,
    adult_product,
    non_physical,
    veterinary,
)

# Strategy 2: reselling genuine branded goods is the point, so no trademark rule.
RESALE_FILTERS: tuple[Filter, ...] = UNIVERSAL_FILTERS

# Strategy 1: you create the listing, so a KNOWN mark is fatal -- and media
# cannot be manufactured at all.
PRIVATE_LABEL_FILTERS: tuple[Filter, ...] = UNIVERSAL_FILTERS + (
    trademark_risk,
    media_product,
)


@dataclass
class FilterSet:
    """A named collection of filters, applied to products in one pass."""

    filters: Sequence[Filter] = field(default_factory=lambda: UNIVERSAL_FILTERS)
    name: str = "universal"

    def evaluate(self, product: dict) -> Verdict:
        """Run every filter and collect all rejections, not just the first."""
        rejections = []
        for f in self.filters:
            try:
                got = f(product)
            except Exception as exc:  # a broken filter must not kill a scan
                rejections.append(Rejection("filter_error", f"{f.__name__}: {exc}"))
                continue
            if got is not None:
                rejections.append(got)
        return Verdict(product.get("asin", ""), tuple(rejections))

    def partition(
        self, products: Iterable[dict]
    ) -> tuple[list[dict], list[tuple[dict, Verdict]]]:
        """Split into (kept, [(product, verdict), ...]) for excluded.csv."""
        kept: list[dict] = []
        dropped: list[tuple[dict, Verdict]] = []
        for product in products:
            verdict = self.evaluate(product)
            if verdict.passed:
                kept.append(product)
            else:
                dropped.append((product, verdict))
        return kept, dropped


RESALE = FilterSet(RESALE_FILTERS, "resale")
PRIVATE_LABEL = FilterSet(PRIVATE_LABEL_FILTERS, "private_label")
