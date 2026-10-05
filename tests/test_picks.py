import pytest
from fastapi.testclient import TestClient

import app as gift_app
from app import GiftRequest, context_phrase, fits_age, name_stem, price_of, recipient, within_budget
from conftest import CATALOG, make_item

client = TestClient(gift_app.app)


def item(name):
    return make_item(next(row for row in CATALOG if row[1] == name))


@pytest.mark.parametrize("person, expected", [
    ("boyfriend", "boyfriend"),
    ("my dad", "dad"),
    ("Pappa", "dad"),
    ("Anna (sister)", "sister"),
    ("best friend Lisa", "best friend"),
    ("mormor", "grandma"),
    ("Anna", ""),
    ("", ""),
])
def test_recipient_is_read_from_who_its_for(person, expected):
    assert recipient(person) == expected


@pytest.mark.parametrize("fields, expected", [
    ({}, ""),
    ({"person": "Dad", "occasion": "birthday"}, "birthday gift for dad"),
    ({"person": "Anna"}, ""),
    ({"person": "my boyfriend", "age": "baby"}, "gift for baby"),  # a child's age wins
    ({"person": "grandpa", "age": "senior"}, "gift for grandpa"),
    ({"age": "senior"}, "gift for seniors"),
    ({"occasion": "housewarming"}, "housewarming gift"),
])
def test_context_phrase(fields, expected):
    assert context_phrase(GiftRequest(interests=["x"], **fields)) == expected


def test_prices_under_ten_are_not_trusted():
    # The catalog stores $1,000+ prices as their thousands digit.
    assert price_of(item("Omega Seamaster Golf Edition Watch")) is None
    assert price_of(item("Birdie Golf Ball Marker")) == 14.0
    assert within_budget(item("Omega Seamaster Golf Edition Watch"), None)
    assert not within_budget(item("Omega Seamaster Golf Edition Watch"), 50)
    assert not within_budget(item("Swing Golf Practice Net"), 50)


def test_age_filter_uses_category_or_name():
    assert fits_age(item("Soft Baby Rattle Set"), "baby")
    assert fits_age(item("Toddler Stacking Rings for 6 Months+"), "baby")  # Toys & Games, but says toddler
    assert not fits_age(item("Omega Seamaster Golf Edition Watch"), "baby")
    assert fits_age(item("Omega Seamaster Golf Edition Watch"), "teen")
    assert fits_age(item("Omega Seamaster Golf Edition Watch"), None)


def test_name_stem_matches_the_same_product_line():
    assert name_stem("Acme Golf Balls 12 Pack") == name_stem("Acme Golf Balls 24 Pack")
    assert name_stem("BBQ Grill Tools Set Gift for Dad") == name_stem("BBQ, Grill Tools: 20 pcs")
    assert name_stem("Acme Golf Balls") != name_stem("Birdie Golf Ball Marker")


def test_product_url_links_the_asin():
    assert gift_app.product_url("paB07L92PYDL", "x") == "https://www.amazon.com/dp/B07L92PYDL"
    assert gift_app.product_url("other-id", "Golf balls").endswith("/s?k=Golf+balls")


def gifts(**body):
    res = client.post("/api/gifts", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def test_first_picks_are_spread_over_interests(fake_client):
    d = gifts(interests=["golf", "grill", "coffee"])
    assert len(d["picks"]) == 3
    assert d["picks"][0]["reason"] == "Top match for their whole profile"
    categories = [p["category"] for p in d["picks"]]
    assert len(set(categories)) == 3


def test_dismissing_skips_near_copies_but_stays_on_the_interest(fake_client):
    d = gifts(interests=["golf"], n=1, focus="golf",
              exclude=["paB000000001"], avoid_similar=["Acme Golf Balls 12 Pack"])
    pick = d["picks"][0]
    assert "golf" in pick["name"].lower()
    assert not pick["name"].startswith("Acme Golf Balls")
    assert pick["seed"] == "golf"


def test_dismissing_does_not_ban_the_whole_category(fake_client):
    # Three golf products in one category are all still reachable.
    seen, avoid = [], []
    for _ in range(3):
        d = gifts(interests=["golf"], n=1, focus="golf", exclude=seen, avoid_similar=avoid)
        pick = d["picks"][0]
        assert pick["category"] == "Sports & Outdoors"
        seen.append(pick["id"]); avoid.append(pick["name"])
    assert len(set(seen)) == 3


def test_saved_products_drive_the_lead_card(fake_client):
    d = gifts(interests=["golf", "grill"], liked=["paB000000007"], n=1)
    assert d["picks"][0]["reason"] == "More like what you saved"
    # The saved product is the latest thing in the history the model sees.
    recs_call = next(h for h in fake_client.calls if type(h[-1]).__name__ != "Search")
    assert [type(e).__name__ for e in recs_call[-2:]] == ["View", "AddToCart"]
    assert recs_call[-1].product == "paB000000007"


def test_baby_age_keeps_only_baby_gifts(fake_client):
    d = gifts(person="boyfriend", interests=["golf"], age="baby")
    assert d["picks"]
    assert all(fits_age(item(p["name"]), "baby") for p in d["picks"])


def test_baby_age_with_unsuitable_pins_falls_back_with_a_note(fake_client, monkeypatch):
    from pinterest import Source
    monkeypatch.setattr(gift_app, "cached_pins", lambda link: (Source("jane"), ["Omega Seamaster Golf Edition Watch"]))
    d = gifts(person="boyfriend", pinterest="jane", age="baby")
    assert d["picks"] and all(p["reason"] == "A gift for baby" for p in d["picks"])
    assert d["note"] == "Nothing in their pins suits a baby, so these are general ideas."


def test_budget_drops_untrusted_prices(fake_client):
    d = gifts(interests=["golf", "watch"], max_price=30)
    names = [p["name"] for p in d["picks"]]
    assert all(p["price"] is not None and p["price"] <= 30 for p in d["picks"])
    assert not any("Omega" in n for n in names)


def test_unknown_interest_is_reported(fake_client):
    d = gifts(interests=["golf", "zzzunknown"])
    assert d["unmatched"] == ["zzzunknown"]


@pytest.mark.parametrize("body", [
    {"interests": []},
    {"interests": ["golf"], "pinterest": "jane"},
])
def test_rejects_missing_or_mixed_inputs(fake_client, body):
    assert client.post("/api/gifts", json=body).status_code == 400


@pytest.mark.parametrize("body", [
    {"interests": ["golf"], "liked": ["../etc/passwd"]},
    {"interests": ["golf"], "occasion": "funeral"},
    {"interests": ["golf"], "n": 4},
])
def test_rejects_invalid_fields(body):
    assert client.post("/api/gifts", json=body).status_code == 422
