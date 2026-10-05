"""Tests run offline: BehaviorGPT is replaced by a tiny fake catalog whose
search ranks products by shared words, which is enough to exercise the
picking logic around the model."""

import re
from types import SimpleNamespace

import pytest
from behaviorgpt import Search
from behaviorgpt.types import Item

import app as gift_app
import lists

CATALOG = [
    # id, name, category, price
    ("paB000000001", "Acme Golf Balls 12 Pack", "Sports & Outdoors", "24"),
    ("paB000000002", "Acme Golf Balls 24 Pack", "Sports & Outdoors", "39"),
    ("paB000000003", "Birdie Golf Ball Marker", "Sports & Outdoors", "14"),
    ("paB000000004", "Swing Golf Practice Net", "Sports & Outdoors", "55"),
    ("paB000000005", "Grillmaster BBQ Tongs Set", "Home & Kitchen", "19"),
    ("paB000000006", "Smokehouse Grill Thermometer", "Home & Kitchen", "29"),
    ("paB000000007", "Pour Over Coffee Kettle", "Home & Kitchen", "45"),
    ("paB000000012", "Barista Coffee Grinder", "Kitchen & Dining", "35"),
    ("paB000000013", "Pitmaster Grill Apron", "Clothing, Shoes & Jewelry", "22"),
    ("paB000000008", "Omega Seamaster Golf Edition Watch", "Clothing, Shoes & Jewelry", "5"),
    ("paB000000009", "Soft Baby Rattle Set", "Baby Products", "12"),
    ("paB000000010", "Toddler Stacking Rings for 6 Months+", "Toys & Games", "15"),
    ("paB000000011", "Baby Gift Basket", "Baby Products", "40"),
]


def make_item(row, score=1.5) -> Item:
    pid, name, category, price = row
    return Item(id=pid, score=score, data={"name": name, "categories": category, "price": price})


class FakeClient:
    """complete() ending on a Search ranks the catalog by words shared with
    the query; otherwise it ranks by words shared with every search so far."""

    def __init__(self):
        self.calls = []

    def complete(self, history, limit=10, **_):
        self.calls.append(history)
        searches = [e.query for e in history if isinstance(e, Search)]
        query = searches[-1] if isinstance(history[-1], Search) else " ".join(searches)
        words = {w for w in re.findall(r"\w+", query.lower()) if len(w) > 2} - {"gift", "for", "accessories"}
        ranked = []
        for row in CATALOG:
            overlap = len(words & set(re.findall(r"\w+", row[1].lower())))
            if overlap:
                ranked.append((overlap, row))
        ranked.sort(key=lambda x: -x[0])
        items = [make_item(row, 1.3 + 0.1 * overlap) for overlap, row in ranked][:limit]
        return SimpleNamespace(products=SimpleNamespace(items=items))


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(gift_app, "_client", client)
    gift_app._search.cache_clear()
    gift_app._pin_cache.clear()
    yield client
    gift_app._search.cache_clear()


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(lists, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(lists, "_ready", False)
