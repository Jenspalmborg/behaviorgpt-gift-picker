"""Gift Picker: turn someone's interests into a behavioral history and let
BehaviorGPT predict what they'd want next.

Run:  uv run uvicorn app:app --reload
"""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from behaviorgpt import Search, UnboxAIClient, View
from behaviorgpt._exceptions import AuthenticationError, UnboxAIError
from behaviorgpt.types import Item
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pinterest import PinterestError, fetch_pins, short_label

load_dotenv(override=True)

CATALOG_ID = os.environ.get("GIFT_CATALOG_ID", "retail_catalog")
MARKET = os.environ.get("GIFT_MARKET", "us")
N_PICKS = 3
# Below this top score, search results are usually unrelated to the query
# (the catalog has nothing for it), so the interest is skipped.
MIN_MATCH_SCORE = 1.30
# The catalog stores prices of $1,000 and up as just their thousands digit
# ("1,299" -> "1"), so a MacBook looks like it costs $1. Real items under this
# can't be told apart from those, so their price is treated as unknown.
MIN_TRUSTED_PRICE = 10.0

_client: UnboxAIClient | None = None


def get_client() -> UnboxAIClient:
    """Created on first use so the page loads even before a key is set."""
    global _client
    if _client is None:
        load_dotenv(override=True)
        _client = UnboxAIClient(market=MARKET, default_catalog_id=CATALOG_ID)
    return _client


app = FastAPI(title="Gift Picker")

STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class GiftRequest(BaseModel):
    person: str = ""
    interests: list[str] = Field(default_factory=list, max_length=8)
    pinterest: str | None = Field(None, max_length=300)
    max_price: float | None = None


class Seed(BaseModel):
    """One thing we know they like: a typed interest or a pin."""

    label: str
    queries: list[str]
    reason: str
    report_if_missing: bool = True


def price_of(item: Item) -> float | None:
    try:
        price = float(item.extract_price(MARKET))
    except (TypeError, ValueError):
        return None
    return price if price >= MIN_TRUSTED_PRICE else None


def within_budget(item: Item, max_price: float | None) -> bool:
    """With a budget set, only items with a price we trust are kept."""
    if max_price is None:
        return True
    price = price_of(item)
    return price is not None and price <= max_price


def category_key(item: Item) -> str:
    """Leaf category, used to keep the three picks from being near-duplicates."""
    cats = item.data.get("categories") or ""
    leaf = cats.split(",")[-1].strip().lower()
    return leaf or item.data.get("brand", "") or item.id


def interest_seed(interest: str) -> Seed:
    """Broad words like "video games" can land on unrelated products, so try a
    few phrasings and keep the one the model is most confident about."""
    return Seed(
        label=interest,
        queries=[interest, f"{interest} gift", f"{interest} accessories"],
        reason=f"For their love of {interest}",
    )


def pin_seed(pin: str) -> Seed:
    """Pin titles are already descriptive, so they're searched as written."""
    return Seed(
        label=pin,
        queries=[pin],
        reason=f"Because they pinned “{short_label(pin)}”",
        report_if_missing=False,
    )


def resolve_seed(seed: Seed, max_price: float | None):
    best_query, best_hits, best_score = seed.queries[0], [], 0.0
    for query in seed.queries:
        res = get_client().complete(history=[Search(query)], limit=10)
        items = res.products.items
        if items and items[0].score > best_score:
            best_score = items[0].score
            best_query = query
            best_hits = [i for i in items if within_budget(i, max_price)]
    return best_query, best_hits, best_score


def build_history(seeds: list[Seed], max_price: float | None):
    """Each seed becomes a search followed by a view of its best match, as if
    the person had browsed a store for the things they love. Seeds come in
    priority order; the history replays them in reverse so the strongest
    signals are the most recent events."""
    with ThreadPoolExecutor(max_workers=min(len(seeds), 12)) as pool:
        resolved = list(pool.map(lambda s: resolve_seed(s, max_price), seeds))

    history = []
    matched: list[tuple[Seed, str, list[Item]]] = []
    unmatched: list[str] = []
    for seed, (query, hits, score) in zip(seeds, resolved):
        if score < MIN_MATCH_SCORE or not hits:
            if seed.report_if_missing:
                unmatched.append(seed.label)
            continue
        matched.append((seed, query, hits))
    for _, query, hits in reversed(matched):
        history += [Search(query), View(hits[0].id)]
    return history, matched, unmatched


def to_card(item: Item, reason: str) -> dict:
    d = item.data
    return {
        "id": item.id,
        "name": d.get("name", "Unknown"),
        "brand": d.get("brand"),
        "category": (d.get("categories") or "").split(",")[-1].strip() or None,
        "image_url": d.get("image_url"),
        "price": price_of(item),
        "currency": d.get("currency") or "USD",
        "score": round(item.score, 4),
        "reason": reason,
    }


def pick_gifts(seeds: list[Seed], max_price: float | None):
    history, matched, unmatched = build_history(seeds, max_price)
    if not history:
        return [], unmatched
    seen_ids = {e.product for e in history if isinstance(e, View)}

    # The model's view of "what does this person want next", given everything.
    recs = get_client().complete(history=history, limit=40).products.items

    picks: list[dict] = []
    used_ids: set[str] = set()
    used_cats: set[str] = set()
    used_stems: set[str] = set()

    def take(item: Item, reason: str) -> bool:
        cat = category_key(item)
        # First few words of the title catch variants like "800 vs 1,700 Robux".
        stem = " ".join(item.data.get("name", "").lower().split()[:4])
        if item.id in used_ids or item.id in seen_ids or cat in used_cats:
            return False
        if stem in used_stems:
            return False
        if not within_budget(item, max_price):
            return False
        picks.append(to_card(item, reason))
        used_ids.add(item.id)
        used_cats.add(cat)
        used_stems.add(stem)
        return True

    # 1) Best blended pick: the top recommendation across all interests.
    for item in recs:
        if take(item, "Top match for their whole profile"):
            break

    # 2) Then one per seed, personalized by the full profile, so the
    #    alternatives cover different things they love.
    for seed, query, _ in matched:
        if len(picks) >= N_PICKS:
            break
        res = get_client().complete(history=[*history, Search(query)], limit=15)
        for item in res.products.items:
            if take(item, seed.reason):
                break

    # 3) Fill any remaining slots from the blended list, then the raw seeds.
    for item in recs:
        if len(picks) >= N_PICKS:
            break
        take(item, "Also fits their profile")
    for seed, _, hits in matched:
        for item in hits:
            if len(picks) >= N_PICKS:
                break
            take(item, seed.reason)

    return picks[:N_PICKS], unmatched


@app.post("/api/gifts")
def gifts(req: GiftRequest):
    interests = [i.strip() for i in req.interests if i.strip()]
    pinterest = None
    seeds = [interest_seed(i) for i in interests]
    if req.pinterest and req.pinterest.strip():
        if interests:
            raise HTTPException(400, "Use either interests or a Pinterest link, not both.")
        try:
            source, pins = fetch_pins(req.pinterest)
        except PinterestError as exc:
            raise HTTPException(400, str(exc)) from exc
        pinterest = {"label": source.label, "url": source.page_url, "pins": len(pins)}
        seeds += [pin_seed(p) for p in pins]
    if not seeds:
        raise HTTPException(400, "Add at least one interest or a Pinterest link.")
    try:
        picks, unmatched = pick_gifts(seeds, req.max_price)
    except AuthenticationError as exc:
        global _client
        _client = None  # pick up a fixed key on the next request
        raise HTTPException(
            401, "No valid UNBOXAI_API_KEY. Add it to .env and try again."
        ) from exc
    except UnboxAIError as exc:
        raise HTTPException(exc.status_code or 502, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Couldn't reach BehaviorGPT: {exc}") from exc
    return {
        "person": req.person,
        "interests": interests,
        "pinterest": pinterest,
        "picks": picks,
        "unmatched": unmatched,
    }


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
