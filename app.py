"""Gift Picker: turn someone's interests into a behavioral history and let
BehaviorGPT predict what they'd want next.

Run:  uv run uvicorn app:app --reload
"""

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

import httpx
from behaviorgpt import AddToCart, Search, UnboxAIClient, View
from behaviorgpt._exceptions import AuthenticationError, UnboxAIError
from behaviorgpt.types import Item
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints

import lists
from lists import product_url
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
PIN_CACHE_SECONDS = 600

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

Occasion = Literal["birthday", "christmas", "anniversary", "wedding", "housewarming", "baby shower", "thank you"]
Age = Literal["baby", "kid", "teen", "adult", "senior"]
ProductId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,40}$")]


class GiftRequest(BaseModel):
    person: str = Field("", max_length=80)
    interests: list[str] = Field(default_factory=list, max_length=8)
    pinterest: str | None = Field(None, max_length=300)
    max_price: float | None = None
    occasion: Occasion | None = None
    age: Age | None = None
    # Refinement: products they saved ("more like this"), products already
    # shown or saved that shouldn't come back, and names of products whose
    # near-copies shouldn't either (ones waved off, ones still on screen).
    liked: list[ProductId] = Field(default_factory=list, max_length=12)
    exclude: list[ProductId] = Field(default_factory=list, max_length=120)
    avoid_similar: list[Annotated[str, StringConstraints(max_length=300)]] = Field(default_factory=list, max_length=40)
    # Replacing a card that came from one interest or pin: draw from that same
    # seed instead of the blended pick, so the slot keeps its theme.
    focus: str | None = Field(None, max_length=300)
    n: int = Field(N_PICKS, ge=1, le=N_PICKS)


class Seed(BaseModel):
    """One thing we know they like: a typed interest or a pin."""

    label: str
    queries: list[str]
    reason: str
    # Short form used to phrase occasion searches, like "golf birthday gift for
    # dad". None for pins: their titles are often long or not in English, and
    # "Barnerom diy housewarming gift" finds nonsense.
    topic: str | None
    report_if_missing: bool = True


_AGE_TERMS = {"baby": "baby", "kid": "kids", "teen": "teen", "senior": "seniors"}

# Words in "Who's it for?" that say who they are to you, as the catalog's
# sellers would phrase it ("gift for boyfriend"). Swedish too, for "pappa".
_RECIPIENTS = {
    "mom": "mom", "mum": "mom", "mother": "mom", "mamma": "mom",
    "dad": "dad", "father": "dad", "pappa": "dad",
    "boyfriend": "boyfriend", "pojkvän": "boyfriend", "girlfriend": "girlfriend", "flickvän": "girlfriend",
    "husband": "husband", "wife": "wife", "fru": "wife", "partner": "partner", "sambo": "partner",
    "fiance": "fiance", "fiancé": "fiance", "fiancee": "fiancee", "fiancée": "fiancee",
    "sister": "sister", "syster": "sister", "brother": "brother", "bror": "brother",
    "friend": "friend", "bestie": "best friend", "vän": "friend", "kompis": "friend",
    "coworker": "coworker", "colleague": "coworker", "kollega": "coworker", "boss": "boss", "chef": "boss",
    "grandma": "grandma", "grandmother": "grandma", "granny": "grandma", "mormor": "grandma", "farmor": "grandma",
    "grandpa": "grandpa", "grandfather": "grandpa", "morfar": "grandpa", "farfar": "grandpa",
    "son": "son", "daughter": "daughter", "dotter": "daughter",
    "aunt": "aunt", "uncle": "uncle", "niece": "niece", "nephew": "nephew", "teacher": "teacher", "lärare": "teacher",
}


def recipient(person: str) -> str:
    """'my boyfriend' -> 'boyfriend', 'Anna (sister)' -> 'sister', 'Anna' -> ''."""
    words = re.findall(r"[^\W\d_]+", person.lower())
    if "best" in words and "friend" in words:
        return "best friend"
    return next((_RECIPIENTS[w] for w in words if w in _RECIPIENTS), "")


# The catalog has no age field. For young children, keep products from
# children's categories, or whose name says who they're for. Teens buy from
# the same shelves as adults, so for them only the search wording changes.
# Plain "Books" and "Toys & Games" aren't enough on their own for a baby:
# "Rolex: 3,621 Wristwatches" is a book.
_CHILD_CATEGORIES = {
    "baby": {"baby products", "baby & toddler toys", "soothers & teethers", "nursery", "children's books"},
    "kid": {"toys & games", "arts, crafts & sewing", "arts & crafts", "building & construction toys",
            "jigsaws & puzzles", "dress up & pretend play", "children's books"},
}
_CHILD_WORDS = {
    "baby": re.compile(r"\b(baby|babies|infants?|newborns?|toddlers?|nursery|\d+\s*months?)\b", re.I),
    "kid": re.compile(r"\b(kids?|children'?s?|boys?|girls?|toddlers?|ages? \d)", re.I),
}


def fits_age(item: Item, age: str | None) -> bool:
    if age not in _CHILD_CATEGORIES:
        return True
    category = (item.data.get("categories") or "").strip().lower()
    return category in _CHILD_CATEGORIES[age] or bool(_CHILD_WORDS[age].search(item.data.get("name", "")))


def context_phrase(req: GiftRequest) -> str:
    """'birthday gift for dad', 'gift for teen', or '' when nothing is set.
    A child's age says more than the relationship ("dad" of a toddler isn't
    the recipient), so baby/kid/teen win over it."""
    who = recipient(req.person)
    if req.age in ("baby", "kid", "teen") or (req.age == "senior" and not who):
        who = _AGE_TERMS[req.age]
    if not (req.occasion or who):
        return ""
    return " ".join(p for p in (req.occasion, "gift", f"for {who}" if who else "") if p)


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


def name_stem(name: str) -> str:
    """First words of a title, which are usually the brand and product line:
    "BBQ Grill Tools Set Gift for Dad" and "BBQ Grill Tools Set, 20 Pcs" match."""
    return " ".join(re.findall(r"\w+", name.lower())[:3])


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
        topic=interest,
    )


def pin_seed(pin: str) -> Seed:
    """Pin titles are already descriptive, so they're searched as written."""
    return Seed(
        label=pin,
        queries=[pin],
        reason=f"Because they pinned “{short_label(pin)}”",
        topic=None,
        report_if_missing=False,
    )


@lru_cache(maxsize=1024)
def _search(query: str) -> tuple[Item, ...]:
    """Plain searches never change for a catalog, so refinements reuse them."""
    return tuple(get_client().complete(history=[Search(query)], limit=10).products.items)


def resolve_seed(seed: Seed, max_price: float | None):
    best_query, best_hits, best_score = seed.queries[0], [], 0.0
    for query in seed.queries:
        items = _search(query)
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


def to_card(item: Item, reason: str, seed: str | None = None) -> dict:
    d = item.data
    name = d.get("name", "Unknown")
    return {
        "id": item.id,
        "name": name,
        "brand": d.get("brand"),
        "category": (d.get("categories") or "").split(",")[-1].strip() or None,
        "image_url": d.get("image_url"),
        "price": price_of(item),
        "currency": d.get("currency") or "USD",
        "url": product_url(item.id, name),
        "score": round(item.score, 4),
        "reason": reason,
        "seed": seed,
    }


def pick_gifts(seeds: list[Seed], req: GiftRequest):
    history, matched, unmatched = build_history(seeds, req.max_price)
    if not history:
        return [], unmatched, None
    seen_ids = {e.product for e in history if isinstance(e, View)}
    # Saving a product is the strongest signal we have, so it goes last. The
    # model leans hard on the latest events, which is why only the first card
    # follows the saves; the rest stay anchored to an interest each.
    for pid in req.liked:
        history += [View(pid), AddToCart(pid)]
    ctx = context_phrase(req)
    n = req.n

    picks: list[dict] = []
    used_ids: set[str] = set(req.exclude) | set(req.liked)
    # Categories are broad ("Sports & Outdoors"), so they only keep the picks
    # within one response apart; across refinements, near-copies are avoided.
    used_cats: set[str] = set()
    used_stems: set[str] = {name_stem(n) for n in req.avoid_similar}

    def take(item: Item, reason: str, seed: str | None = None) -> bool:
        cat = category_key(item)
        # Catches variants like "800 vs 1,700 Robux" and the same set from one brand.
        stem = name_stem(item.data.get("name", ""))
        if item.id in used_ids or item.id in seen_ids or cat in used_cats:
            return False
        if stem in used_stems:
            return False
        if not within_budget(item, req.max_price) or not fits_age(item, req.age):
            return False
        picks.append(to_card(item, reason, seed))
        used_ids.add(item.id)
        used_cats.add(cat)
        used_stems.add(stem)
        return True

    def personalized(query: str, limit: int = 15) -> list[Item]:
        return get_client().complete(history=[*history, Search(query)], limit=limit).products.items

    # The model's view of "what does this person want next", given everything.
    recs = get_client().complete(history=history, limit=40).products.items

    focused = [m for m in matched if m[0].label == req.focus]
    if focused:
        matched = focused + [m for m in matched if m[0].label != req.focus]
    else:
        # 1) Best blended pick: the top recommendation across all interests.
        lead = "More like what you saved" if req.liked else "Top match for their whole profile"
        for item in recs:
            if take(item, lead):
                break

    # 2) Then one per seed, personalized by the full profile, so the
    #    alternatives cover different things they love. With an occasion or
    #    recipient set, the search says so: "golf birthday gift for dad".
    #    Pins can't be phrased that way, so they leave the last slot for a
    #    search on the occasion alone, still personalized by their pins.
    #    A few spare seeds are searched in case some only repeat earlier picks.
    occasion_slot = bool(ctx) and any(seed.topic is None for seed, _, _ in matched)
    seed_slots = n - 1 if occasion_slot and n > 1 else n
    candidates = matched[: n + 3]
    with ThreadPoolExecutor(max_workers=len(candidates) + 1) as pool:
        queries = [f"{seed.topic} {ctx}" if ctx and seed.topic else query for seed, query, _ in candidates]
        per_seed = pool.map(personalized, queries)
        for_occasion = pool.submit(personalized, ctx) if occasion_slot else None
        for (seed, _, _), items in zip(candidates, per_seed):
            if len(picks) >= seed_slots:
                break
            for item in items:
                if take(item, seed.reason, seed.label):
                    break
        if for_occasion:
            for item in for_occasion.result():
                if len(picks) >= n or take(item, f"A {ctx}"):
                    break

    # 3) Fill any remaining slots from the blended list, then the raw seeds.
    for item in recs:
        if len(picks) >= n:
            break
        take(item, "Also fits their profile")
    for seed, _, hits in matched:
        for item in hits:
            if len(picks) >= n:
                break
            take(item, seed.reason, seed.label)

    # 4) Still short, usually because nothing they like fits the age set:
    #    search the occasion or recipient alone, personalized by the profile.
    general = f"A {ctx}"
    if len(picks) < n and ctx:
        for item in personalized(ctx, limit=30):
            if len(picks) >= n:
                break
            take(item, general)
    note = None
    if req.age in _CHILD_CATEGORIES and picks and all(p["reason"] == general for p in picks):
        note = f"Nothing in their {'pins' if matched[0][0].topic is None else 'interests'} suits a {_AGE_TERMS[req.age].rstrip('s')}, so these are general ideas."

    return picks[:n], unmatched, note


_pin_cache: dict[str, tuple[float, tuple]] = {}


def cached_pins(link: str):
    """Refining picks re-sends the same link, so keep its pins for a while."""
    key = link.strip().lower()
    hit = _pin_cache.get(key)
    if hit and time.monotonic() - hit[0] < PIN_CACHE_SECONDS:
        return hit[1]
    result = fetch_pins(link)
    _pin_cache[key] = (time.monotonic(), result)
    return result


@app.post("/api/gifts")
def gifts(req: GiftRequest):
    interests = [i.strip() for i in req.interests if i.strip()]
    pinterest = None
    seeds = [interest_seed(i) for i in interests]
    if req.pinterest and req.pinterest.strip():
        if interests:
            raise HTTPException(400, "Use either interests or a Pinterest link, not both.")
        try:
            source, pins = cached_pins(req.pinterest)
        except PinterestError as exc:
            raise HTTPException(400, str(exc)) from exc
        pinterest = {"label": source.label, "url": source.page_url, "pins": len(pins)}
        seeds += [pin_seed(p) for p in pins]
    if not seeds:
        raise HTTPException(400, "Add at least one interest or a Pinterest link.")
    try:
        picks, unmatched, note = pick_gifts(seeds, req)
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
        "occasion": req.occasion,
        "picks": picks,
        "unmatched": unmatched,
        "note": note,
    }


app.include_router(lists.router)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/list/{list_id}")
def list_page(list_id: str):
    return FileResponse(STATIC / "list.html")
