import pytest
from fastapi.testclient import TestClient

import app as gift_app
from app import GiftRequest, context_phrase, fits_age, giftable, name_words, near_copy, price_of, within_budget
from conftest import CATALOG, FakeClient, make_item

client = TestClient(gift_app.app)


def item(name):
    return make_item(next(row for row in CATALOG if row[1] == name))


@pytest.mark.parametrize("fields, expected", [
    ({}, ""),
    ({"person": "Dad"}, ""),  # who it's for never steers the picks
    ({"person": "Dad", "occasion": "birthday"}, "birthday gift"),
    ({"person": "my boyfriend", "age": "baby"}, "gift for baby"),
    ({"occasion": "christmas", "age": "kid"}, "christmas gift for kids"),
    ({"age": "adult"}, ""),
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


@pytest.mark.parametrize("a, b, same", [
    ("Acme Golf Balls 12 Pack", "Acme Golf Balls 24 Pack", True),
    ("MASTER FENG Sausage Stuffer, Stainless Steel", "Sausage Stuffer - Stainless Steel Homemade", True),
    ("Borla 140597 Cat-Back Exhaust", "BORLA 140753 Cat-Back Perf. Exhaust", True),
    ("Acme Golf Balls 12 Pack", "Birdie Golf Ball Marker", False),
    ("Callaway Golf 2021 Supersoft Golf Balls", "Personalised Golf Balls and Tees Gift Set", False),
])
def test_near_copies_match_in_any_word_order(a, b, same):
    assert near_copy(name_words(a), name_words(b)) is same


def test_groceries_and_supplies_only_count_as_gifts_when_sold_as_one():
    def thing(name, category):
        return make_item(("x", name, category, "20"))
    assert not giftable(thing("Ball Park Frozen Beef Patties", "Grocery & Gourmet Food"))
    assert not giftable(thing("Rubber Stair Treads Non-Slip", "Tools & Home Improvement"))
    assert giftable(thing("The Bon Appetit Gourmet Gift Basket", "Grocery & Gourmet Food"))
    assert giftable(thing("Craftsman 230-Piece Tool Set", "Tools & Home Improvement"))
    assert giftable(thing("Pour Over Coffee Kettle", "Home & Kitchen"))


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
    d = gifts(interests=["golf", "coffee"], n=1, focus="golf",
              exclude=["paB000000001"], avoid_similar=["Acme Golf Balls 12 Pack"])
    pick = d["picks"][0]
    assert "golf" in pick["name"].lower()
    assert not pick["name"].startswith("Acme Golf Balls")
    assert pick["group"] == "golf"


def test_dismissing_does_not_ban_the_whole_category(fake_client):
    # Three golf products in one category are all still reachable.
    seen, avoid = [], []
    for _ in range(3):
        d = gifts(interests=["golf"], n=1, focus="golf", exclude=seen, avoid_similar=avoid)
        pick = d["picks"][0]
        assert pick["category"] == "Sports & Outdoors"
        seen.append(pick["id"]); avoid.append(pick["name"])
    assert len(set(seen)) == 3


def test_who_its_for_does_not_reserve_a_card(fake_client):
    d = gifts(person="Dad", interests=["golf", "grill", "coffee"])
    assert not any(p["reason"].startswith("A ") for p in d["picks"])
    assert all("dad" not in " ".join(map(str, h)).lower() for h in fake_client.calls)


def test_show_others_moves_on_to_interests_not_shown_recently(fake_client):
    interests = ["golf", "grill", "coffee", "baby"]
    first = gifts(interests=interests)
    shown_groups = [p["group"] for p in first["picks"] if p["group"]]
    unseen = set(interests) - set(shown_groups)
    assert unseen
    again = gifts(interests=interests, exclude=[p["id"] for p in first["picks"]], recent_groups=shown_groups)
    assert unseen & {p["group"] for p in again["picks"]}


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


def test_one_card_per_board(fake_client, monkeypatch):
    from pinterest import Pin, Source
    pins = [Pin("Acme Golf Balls", "Golf"), Pin("Birdie Golf Ball Marker", "Golf"),
            Pin("Swing Golf Practice Net", "Golf"), Pin("Pour Over Coffee Kettle", "Kitchen")]
    monkeypatch.setattr(gift_app, "cached_profile", lambda link: (Source("jane"), pins, []))
    d = gifts(pinterest="jane")
    groups = [p["group"] for p in d["picks"] if p["group"]]
    assert len(groups) == len(set(groups))


def test_baby_age_with_unsuitable_pins_falls_back_with_a_note(fake_client, monkeypatch):
    from pinterest import Pin, Source
    monkeypatch.setattr(gift_app, "cached_profile", lambda link: (Source("jane"), [Pin("Omega Seamaster Golf Edition Watch", "Watches")], []))
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


class KitchenBiasedClient(FakeClient):
    """Like the real model with a big food board: every personalized search
    puts kitchen products first, whatever was searched."""

    def complete(self, history, limit=10, **kw):
        res = super().complete(history, limit, **kw)
        if len(history) > 1:
            kitchen = [make_item(r, 1.6) for r in CATALOG if r[2] == "Home & Kitchen"]
            res.products.items = kitchen + res.products.items
        return res


def test_board_cards_stay_on_their_own_topic(monkeypatch):
    from pinterest import Board, Pin, Source
    monkeypatch.setattr(gift_app, "_client", KitchenBiasedClient())
    gift_app._search.cache_clear()
    boards = [Board("Mat", 40, ("coffee",)), Board("Prylar", 3, ("golf practice",))]
    pins = [Pin("Pour Over Coffee Kettle", "Mat"), Pin("Smokehouse Grill Thermometer", "Mat")]
    monkeypatch.setattr(gift_app, "cached_profile", lambda link: (Source("jane"), pins, boards))
    d = gifts(pinterest="jane", n=1, focus="Prylar")
    pick = d["picks"][0]
    assert pick["group"] == "Prylar"
    assert pick["reason"] == "For their love of golf practice"
    assert pick["category"] == "Sports & Outdoors"


def test_theme_seed_tries_each_topic_as_gift_and_accessories():
    from pinterest import Board
    seed = gift_app.theme_seed(Board("Prag", 4, ("prague", "places to travel")))
    assert seed.queries == ["prague", "prague gift", "prague accessories",
                            "places to travel", "places to travel gift", "places to travel accessories"]
    assert gift_app.reason_for(seed, "places to travel accessories") == "For their love of places to travel"
    watches = gift_app.theme_seed(Board("Watches", 15, ("luxury watches", "mens accessories")))
    assert gift_app.topic_of(watches, "mens accessories") == "mens accessories"
    assert gift_app.topic_of(watches, "mens accessories gift") == "mens accessories"


def test_top_match_counts_as_its_boards_card(fake_client):
    rows = {r[1]: r for r in CATALOG}
    watch = make_item(rows["Omega Seamaster Golf Edition Watch"])
    seeds = [gift_app.interest_seed("golf"), gift_app.interest_seed("coffee")]
    _, matched, _ = gift_app.build_history(seeds, None)
    assert gift_app.closest_group(watch, matched) == "golf"
    kettle = make_item(rows["Pour Over Coffee Kettle"])
    assert gift_app.closest_group(kettle, matched) == "coffee"


def test_theme_prefers_a_phrasing_that_fits_the_rest_of_the_board():
    from pinterest import Board

    def result(name, category, score):
        return (make_item(("x" + name[:5], name, category, "20"), score),)
    seed = gift_app.theme_seed(Board("Inspo", 1, ("stairs", "furniture", "home decor")))
    results = {q: () for q in seed.queries}
    results["stairs accessories"] = result("Rubber Stair Treads", "Tools & Home Improvement", 1.44)
    results["furniture"] = result("Sofa", "Home & Kitchen", 1.39)
    results["home decor accessories"] = result("Table Fountain", "Home & Kitchen", 1.42)
    query, score = gift_app.best_query(seed, results, set())
    assert query == "home decor accessories" and score == 1.42
    # On its own merits (no fit bonus), stairs would have won.
    assert gift_app.best_query(gift_app.interest_seed("stairs"), {"stairs": results["stairs accessories"],
                               "stairs gift": (), "stairs accessories": ()}, set())[0] == "stairs"


def test_top_match_is_on_topic_and_giftable(monkeypatch):
    class OffTopicRecs(FakeClient):
        """Recommendations (no final search) lead with a stray grocery item."""
        def complete(self, history, limit=10, **kw):
            res = super().complete(history, limit, **kw)
            if type(history[-1]).__name__ != "Search":
                res.products.items = [make_item(("paX", "Olive Hummus 10 Oz", "Grocery & Gourmet Food", "4"), 1.9),
                                      *res.products.items]
            return res
    monkeypatch.setattr(gift_app, "_client", OffTopicRecs())
    gift_app._search.cache_clear()
    d = gifts(interests=["golf", "coffee"])
    assert d["picks"][0]["reason"] == "Top match for their whole profile"
    assert "Hummus" not in d["picks"][0]["name"]
