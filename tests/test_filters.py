"""Tests for the negative filters.

The bug class these guard against is the false positive. A filter that quietly
rejects good rows is worse than no filter, because the shortlist still looks
plausible -- it is just missing the candidates you wanted. Substring matching is
the usual culprit, so most of this file is about word boundaries.
"""

import pytest

from core import filters
from core.filters import FilterSet, Rejection


def product(**kw) -> dict:
    base = {"asin": "B000TEST01", "title": "Plain Storage Box", "brand": "Generic"}
    base.update(kw)
    return base


# -- normalisation and matching ------------------------------------------


def test_normalise_strips_punctuation_and_case():
    assert filters.normalise("Levi's  JEANS!") == "levis jeans"


def test_apostrophe_forms_unify():
    """So the brand list need not carry both spellings."""
    assert filters.normalise("Levi's") == filters.normalise("Levis")


def test_matching_is_word_boundary_not_substring():
    """The whole reason this module compiles regexes instead of using `in`."""
    assert not filters.contains_phrase("masonry drill bit", "sony")
    assert not filters.contains_phrase("toxic waste sign", "oxo")
    assert not filters.contains_phrase("pineapple corer", "apple")
    assert filters.contains_phrase("sony headphones", "sony")


def test_multiword_phrases_match():
    assert filters.contains_phrase("the north face jacket", "the north face")
    assert not filters.contains_phrase("north facing window", "the north face")


def test_searchable_text_excludes_description():
    """Descriptions name competitors and unrelated products, causing false
    rejections."""
    p = product(title="Storage Box", description="better than a LEGO bin")
    assert "lego" not in filters.searchable_text(p)


def test_searchable_text_includes_features():
    p = product(features=["Contains a lithium battery"])
    assert "lithium" in filters.searchable_text(p)


# -- gated brands ---------------------------------------------------------


def test_gated_brand_matches_brand_field():
    r = filters.gated_brand(product(brand="LEGO"))
    assert r and r.filter_name == "gated_brand"


def test_gated_brand_matches_manufacturer():
    assert filters.gated_brand(product(brand="Generic", manufacturer="Nike"))


def test_gated_brand_matches_title_when_brand_field_is_generic():
    """Catches ASINs whose brand field is vague but whose title names the mark."""
    r = filters.gated_brand(product(brand="Generic", title="Genuine Apple Charger"))
    assert r and "apple" in r.detail.lower()


def test_gated_brand_does_not_fire_on_lookalike_words():
    assert filters.gated_brand(product(title="Pineapple Slicer")) is None
    assert filters.gated_brand(product(title="Masonry Drill Set")) is None


def test_gated_brand_list_loads_and_ignores_comments(tmp_path):
    f = tmp_path / "brands.txt"
    f.write_text("# a comment\n\nNike\nAdidas\n", encoding="utf-8")
    filters.load_gated_brands.cache_clear()
    assert filters.load_gated_brands(str(f)) == ("Nike", "Adidas")
    filters.load_gated_brands.cache_clear()


# -- IP-hot ---------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    ["Funko Pop Batman", "Pokemon Card Binder", "Signed Football Shirt"],
)
def test_ip_hot_catches_licensed_and_memorabilia(title):
    assert filters.ip_hot(product(title=title))


def test_ip_hot_leaves_plain_goods_alone():
    assert filters.ip_hot(product(title="Bamboo Drawer Organiser")) is None


# -- compliance -----------------------------------------------------------


def test_cosmetics_caught_for_cpsr():
    r = filters.compliance_heavy(product(title="Vitamin C Face Serum"))
    assert r and r.filter_name == "compliance_cosmetics"


def test_supplements_caught_for_fsa():
    r = filters.compliance_heavy(product(title="Whey Protein Powder 1kg"))
    assert r and r.filter_name == "compliance_supplements"


def test_medical_devices_caught():
    r = filters.compliance_heavy(product(title="Digital Blood Pressure Monitor"))
    assert r and r.filter_name == "compliance_medical"


def test_ppe_caught():
    r = filters.compliance_heavy(product(title="FFP3 Respirator Mask"))
    assert r and r.filter_name == "compliance_ppe"


def test_toys_only_caught_when_an_age_marker_is_present():
    """A toy alone is not evidence of the under-14 regime; a toy plus an age
    marker is. Rejecting every 'puzzle' would gut the categories worth scanning."""
    adult = product(title="Wooden Puzzle Box for Adults")
    kids = product(title="Toy Building Blocks for Toddler Age 3")
    assert filters.compliance_heavy(adult) is None
    r = filters.compliance_heavy(kids)
    assert r and r.filter_name == "compliance_toys_under_14"


def test_mains_electricals_caught_for_ukca():
    r = filters.compliance_heavy(product(title="2000W Electric Heater UK Plug"))
    assert r and r.filter_name == "compliance_ukca"


def test_plain_homeware_passes_compliance():
    assert filters.compliance_heavy(product(title="Bamboo Drawer Organiser")) is None


# -- hazmat ---------------------------------------------------------------


def test_keepa_hazmat_flag_is_authoritative():
    p = product(hazardousMaterials=[{"aspect": "flammable", "value": "class 3"}])
    r = filters.hazmat(p)
    assert r and "flammable" in r.detail


def test_batteries_included_is_hazmat():
    r = filters.hazmat(product(batteriesIncluded=True))
    assert r and r.filter_name == "hazmat_batteries"


def test_batteries_required_alone_is_not_hazmat():
    """Requiring batteries the buyer supplies does not ship a battery."""
    assert filters.hazmat(product(batteriesRequired=True)) is None


def test_hazmat_keywords_and_safety_warning():
    assert filters.hazmat(product(title="Butane Gas Refill"))
    assert filters.hazmat(product(safetyWarning="Contents under pressure, flammable"))


def test_ordinary_product_is_not_hazmat():
    assert filters.hazmat(product(title="Cotton Tea Towels")) is None


# -- physical -------------------------------------------------------------


def test_overweight_rejected():
    r = filters.physical_limits(product(packageWeight=2500))
    assert r and r.filter_name == "too_heavy"


def test_oversize_rejected_on_longest_side():
    r = filters.physical_limits(
        product(packageLength=500, packageWidth=100, packageHeight=100)
    )
    assert r and "500mm" in r.detail


def test_unknown_dimensions_pass_rather_than_reject():
    """Keepa uses -1 for unknown. Rejecting on unknown would discard a large
    share of good ASINs; fees.postage_for() catches genuinely unpostable items
    later, before any profit number is produced."""
    p = product(packageWeight=-1, packageLength=-1, packageWidth=-1, packageHeight=-1)
    assert filters.physical_limits(p) is None


def test_within_limits_passes():
    p = product(packageWeight=400, packageLength=300, packageWidth=200, packageHeight=50)
    assert filters.physical_limits(p) is None


def test_fragile_and_adult():
    assert filters.fragile(product(title="Crystal Wine Glasses Set"))
    assert filters.adult_product(product(isAdultProduct=True))
    assert filters.adult_product(product(isAdultProduct=False)) is None


# -- trademark, and why it is not universal ------------------------------


def test_trademark_rejects_known_marks():
    r = filters.trademark_risk(product(brand="LEGO"))
    assert r and r.filter_name == "trademark_risk"


def test_trademark_rejects_protected_ip_in_the_title():
    r = filters.trademark_risk(product(title="Pokemon Card Album", brand="Generic"))
    assert r and "protected IP" in r.detail


def test_trademark_allows_an_unknown_generic_incumbent():
    """The deviation from a literal reading of the brief, and why.

    Rejecting every ASIN that has a brand field killed 29 of 49 rows on the
    first real scan -- essentially every Amazon listing carries a brand, since
    generic Chinese sellers register names too. A branded incumbent does not
    stop you selling a competing bamboo organiser under your OWN brand.
    """
    assert filters.trademark_risk(product(brand="JEKEMORYE")) is None
    assert filters.trademark_risk(product(brand="Generic")) is None


def test_media_cannot_be_private_labelled():
    """An unscoped scan returned vinyl and Blu-rays: they pass every niche
    filter and cannot be manufactured."""
    vinyl = product(title="Stardust (Translucent Cobalt Blue Vinyl)",
                    type="ABIS_MUSIC")
    assert filters.media_product(vinyl)
    assert not filters.PRIVATE_LABEL.evaluate(vinyl).passed
    assert filters.media_product(product(title="Bamboo Drawer Organiser")) is None


def test_resale_set_permits_branded_goods():
    """Strategy 2 resells genuine branded goods bought from Amazon -- lawful,
    and the whole strategy. Applying the trademark rule there would reject
    everything."""
    branded = product(brand="LEGO", title="LEGO Kitchen Scales")
    assert not filters.PRIVATE_LABEL.evaluate(branded).passed


# -- filter set behaviour -------------------------------------------------


def test_all_rejections_collected_not_just_the_first():
    p = product(brand="LEGO", title="LEGO Star Wars Set", packageWeight=3000)
    verdict = filters.RESALE.evaluate(p)
    names = {r.filter_name for r in verdict.rejections}
    assert {"gated_brand", "ip_hot", "too_heavy"} <= names
    assert verdict.primary.filter_name == "gated_brand"
    assert "gated_brand" in verdict.reason()


def test_clean_product_passes_everything():
    p = product(
        title="Bamboo Drawer Organiser 4 Compartment",
        brand="Generic",
        packageWeight=480,
        packageLength=300,
        packageWidth=200,
        packageHeight=60,
    )
    assert filters.RESALE.evaluate(p).passed
    assert filters.PRIVATE_LABEL.evaluate(p).passed


def test_partition_splits_and_keeps_reasons():
    good = product(title="Bamboo Drawer Organiser", packageWeight=400)
    bad = product(asin="B0BAD", title="Nike Trainers", brand="Nike")
    kept, dropped = filters.RESALE.partition([good, bad])
    assert [p["asin"] for p in kept] == ["B000TEST01"]
    assert dropped[0][1].primary.filter_name == "gated_brand"


def test_a_broken_filter_does_not_kill_the_scan():
    def exploding(_p):
        raise RuntimeError("boom")

    fs = FilterSet((exploding,), "broken")
    verdict = fs.evaluate(product())
    assert not verdict.passed
    assert verdict.primary.filter_name == "filter_error"
    assert "boom" in verdict.primary.detail


def test_empty_product_does_not_crash():
    assert isinstance(filters.RESALE.evaluate({}), filters.Verdict)


# -- gaps found by the first live Strategy 1 scan -------------------------


def test_cpap_consumables_are_medical():
    """Ranked SECOND on the first S1 scan. A consumable for a medical device
    carries the same regime as the device."""
    r = filters.compliance_heavy(
        product(title="ResMed AirTouch F20 Full Face Replacement Cushion - Large")
    )
    assert r and r.filter_name == "compliance_medical"


def test_sim_cards_and_services_are_not_products():
    """Ranked FOURTH on the first S1 scan. Passes every numeric filter -- it
    sells steadily, stable rank, few reviews, no weight -- and cannot be
    sourced, shipped or private-labelled."""
    r = filters.non_physical(product(title="Vodafone Unlimited Data SIM Card UK"))
    assert r and r.filter_name == "non_physical"
    for title in ("Amazon Gift Card £50", "Xbox Game Pass 3 Month Digital Code",
                  "3 Year Extended Warranty"):
        assert filters.non_physical(product(title=title)), title


def test_ordinary_goods_are_still_physical():
    assert filters.non_physical(product(title="Bamboo Drawer Organiser")) is None
    assert filters.non_physical(product(title="48mm Jockey Wheel and Clamp")) is None


def test_zero_weight_means_unknown_not_weightless():
    """Keepa stores 0 as well as -1 for 'not known'. Treating 0 as a real
    measurement let four 15-litre water bottles through a 700g filter."""
    assert filters.known_measure(0) is None
    assert filters.known_measure(-1) is None
    assert filters.known_measure(480) == 480
    p = product(packageWeight=0, packageLength=0, packageWidth=0, packageHeight=0)
    assert filters.physical_limits(p) is None, "unknown passes locally..."


# -- gaps found by the first SCOPED Strategy 1 scan -----------------------


def test_child_life_jacket_is_ppe():
    """Top-scoring row on a live scan. A child's buoyancy aid is UKCA-marked
    life-saving equipment and about as liable as a first sale can get."""
    r = filters.compliance_heavy(product(title="TWF FREEDOM LIFE JACKET 10-20Kg"))
    assert r and r.filter_name == "compliance_ppe"


@pytest.mark.parametrize("title", [
    "Chanelle Prazitel Plus Flavour Wormer for Dogs",
    "BARRIER BLOWFLY REPEL FOR SHEEP",
    "MalAcetic Aural Ear and Skin Cleanser 4 oz",
])
def test_veterinary_medicines_rejected(title):
    """Pet Supplies was the densest category and was full of these."""
    assert filters.veterinary(product(title=title)), title


def test_veterinary_caught_by_category_when_the_title_is_opaque():
    p = product(title="Animed Direct Enisyl F Paste For Cats 100ml",
                categoryTree=[{"name": "Pet Supplies"},
                              {"name": "Nonprescription Medications"}])
    assert filters.veterinary(p)


def test_ordinary_pet_goods_still_pass():
    """The filter must not swallow the whole category -- beds, bowls, leads and
    toys are exactly the commodity goods worth private-labelling."""
    for title in ("Dog Bed Washable Large", "Stainless Steel Dog Bowl",
                  "Nylon Dog Lead 2m"):
        assert filters.veterinary(product(title=title)) is None, title


def test_lpg_fittings_are_hazmat():
    assert filters.hazmat(product(title="Gomet M22 LPG GPL Autogas Tank Refill Adapter"))


def test_personal_care_caught_by_category_when_the_title_is_clean():
    """Topped a Strategy 3 run. The title names no regulated term, but the
    category path is Intimate Hygiene and the features promise pain relief."""
    p = product(title="Frida Mom Postpartum Recovery Essentials Kit",
                categoryTree=[{"name": "Health & Personal Care"},
                              {"name": "Intimate Hygiene"},
                              {"name": "Intimate Care"}])
    assert filters.compliance_heavy(p)


def test_medicinal_claims_are_caught():
    assert filters.compliance_heavy(product(title="Cooling Pain Relief Spray"))


def test_plain_goods_survive_the_new_category_rules():
    assert filters.compliance_heavy(
        product(title="Bamboo Drawer Organiser",
                categoryTree=[{"name": "Home & Garden"}, {"name": "Storage"}])
    ) is None
