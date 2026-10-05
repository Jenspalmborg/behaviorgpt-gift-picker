"""Gift Picker: turn someone's interests into a behavioral history and let
BehaviorGPT predict what they'd want next.

Run:  uv run uvicorn app:app --reload
"""

import os
import re
import time
from collections import defaultdict
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
from pinterest import Board, Pin, PinterestError, fetch_profile, short_label

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
# How much a board theme's fit with the rest of the board counts next to the
# model's own score (which spans roughly 1.30-1.52 for useful matches).
THEME_FIT_WEIGHT = 0.1

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
    # Replacing a card that came from one interest or board: draw from that
    # same group instead of the blended pick, so the slot keeps its theme.
    focus: str | None = Field(None, max_length=300)
    # Groups (interests or boards) shown recently, so new cards move on to
    # the others instead of starting over at the first one.
    recent_groups: list[Annotated[str, StringConstraints(max_length=300)]] = Field(default_factory=list, max_length=40)
    n: int = Field(N_PICKS, ge=1, le=N_PICKS)


class Seed(BaseModel):
    """One thing we know they like: a typed interest, a board's theme (from
    Pinterest's topics for it), or a single pin."""

    kind: Literal["interest", "theme", "pin"]
    label: str
    queries: list[str]
    # What it's one of: the interest itself, or the board. A screenful shows
    # at most one card per group.
    group: str
    # Pins only add to the history; interests and themes also get cards,
    # since their wording is a topic ("fixie bike") rather than a caption.
    reason: str = ""
    # Themes: Pinterest's topics, each searched as is, as a gift and as
    # accessories (the queries above).
    topics: list[str] = []


_SUFFIXES = (" gift", " accessories")


def topic_of(seed: Seed, query: str) -> str | None:
    """The plain topic behind a seed's best query: "fixie bike accessories"
    -> "fixie bike". None for pins: their titles are often long or not in
    English, and "Barnerom diy housewarming gift" finds nonsense."""
    if seed.kind == "pin":
        return None
    # Matched against the seed's own topics, since a topic can itself end in
    # a suffix: "mens accessories" is one of Pinterest's watch topics.
    for topic in seed.topics or [seed.label]:
        if query == topic or any(query == topic + suffix for suffix in _SUFFIXES):
            return topic
    return query


def reason_for(seed: Seed, query: str) -> str:
    return seed.reason or f"For their love of {topic_of(seed, query)}"


_AGE_TERMS = {"baby": "baby", "kid": "kids", "teen": "teen", "senior": "seniors"}

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
    """'birthday gift for kids', 'christmas gift', or '' when nothing is set.
    Only what was picked explicitly counts: "Who's it for?" stays out, since
    a dad might want LEGO as much as a grill."""
    who = _AGE_TERMS.get(req.age or "", "")
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


_STOPWORDS = {"with", "for", "and", "the", "set", "pack", "pcs", "piece", "gift", "gifts",
              "men", "women", "mens", "womens", "from", "your", "you", "new"}


def _words(name: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", name.lower()) if w not in _STOPWORDS}


def name_words(name: str) -> set[str]:
    """The main words at the start of a title, where brand and product type sit."""
    return _words(" ".join(name.split()[:8]))


def near_copy(a: set[str], b: set[str]) -> bool:
    """Same kind of product, in any word order: "MASTER FENG Sausage Stuffer,
    Stainless Steel" and "Sausage Stuffer - Stainless Steel Homemade" share
    four main words. Golf balls and a golf ball marker share only "golf"."""
    shared = len(a & b)
    return shared >= 2 and shared >= min(3, len(a), len(b))


# Groceries, household supplies and repair parts make poor gifts (frozen
# patties, motion-sickness patches, stair treads), unless sold as a gift.
_LOW_GIFT_CATEGORIES = {"grocery & gourmet food", "health & household", "household supplies",
                        "tools & home improvement"}
_GIFT_WORDS = re.compile(r"\b(gifts?|baskets?|hampers?|box|sets?|kits?|sampler|collection)\b", re.I)


def giftable(item: Item) -> bool:
    category = (item.data.get("categories") or "").strip().lower()
    return category not in _LOW_GIFT_CATEGORIES or bool(_GIFT_WORDS.search(item.data.get("name", "")))


def gifts_first(items) -> list[Item]:
    """Same order, giftable products ahead of the rest."""
    return sorted(items, key=lambda i: not giftable(i))


def category_key(item: Item) -> str:
    """Leaf category, used to keep the three picks from being near-duplicates."""
    cats = item.data.get("categories") or ""
    leaf = cats.split(",")[-1].strip().lower()
    return leaf or item.data.get("brand", "") or item.id


def interest_seed(interest: str) -> Seed:
    """Broad words like "video games" can land on unrelated products, so try a
    few phrasings and keep the one the model is most confident about."""
    return Seed(
        kind="interest",
        label=interest,
        queries=[interest, f"{interest} gift", f"{interest} accessories"],
        reason=f"For their love of {interest}",
        group=interest,
    )


def theme_seed(board: Board) -> Seed:
    """A board as one theme: Pinterest's topics for it, each tried as is, as
    a gift and as accessories ("places to travel accessories" finds a luggage
    scale), keeping the phrasing the model is most confident about."""
    return Seed(
        kind="theme",
        label=board.name,
        queries=[q for t in board.topics for q in (t, *(t + s for s in _SUFFIXES))],
        group=board.name,
        topics=list(board.topics),
    )


def pin_seed(pin: Pin) -> Seed:
    """Pin titles are already descriptive, so they're searched as written."""
    return Seed(
        kind="pin",
        label=pin.text,
        queries=[pin.text],
        reason=f"Because they pinned “{short_label(pin.text)}”",
        group=pin.board or pin.text,
    )


@lru_cache(maxsize=1024)
def _search(query: str) -> tuple[Item, ...]:
    """Plain searches never change for a catalog, so refinements reuse them."""
    return tuple(get_client().complete(history=[Search(query)], limit=10).products.items)


_SEARCH_POOL = ThreadPoolExecutor(max_workers=24)


def best_query(seed: Seed, results: dict[str, tuple[Item, ...]], board_cats: set) -> tuple[str, float]:
    """The phrasing to use for a seed, and its score. For interests and pins,
    the one the model is most confident about. For a board theme, that score
    plus how well the phrasing fits the rest of the board: the share of its
    other topics (and of its pins' matches) whose results include the same
    category. Inspo's topics agree on Home & Kitchen, so "home decor" beats
    the slightly higher-scoring but lone "stairs" (stair treads)."""
    def score(q):
        return results[q][0].score if results.get(q) else 0.0

    if seed.kind != "theme":
        q = max(seed.queries, key=score)
        return q, score(q)

    cats_by_topic = {
        t: {i.data.get("categories") for v in (t, *(t + s for s in _SUFFIXES)) for i in results.get(v, ())[:5]}
        for t in seed.topics
    }

    def fit(q):
        if not results.get(q):
            return 0.0
        topic = topic_of(seed, q)
        top_cat = results[q][0].data.get("categories")
        others = [c for t, c in cats_by_topic.items() if t != topic] + ([board_cats] if board_cats else [])
        return sum(top_cat in c for c in others) / len(others) if others else 0.0

    q = max(seed.queries, key=lambda q: score(q) + THEME_FIT_WEIGHT * fit(q))
    return q, score(q)


def build_history(seeds: list[Seed], max_price: float | None):
    """Each seed becomes a search followed by a view of its best match, as if
    the person had browsed a store for the things they love. Seeds come in
    priority order; the history replays them in reverse so the strongest
    signals are the most recent events."""
    queries = list(dict.fromkeys(q for seed in seeds for q in seed.queries))
    results = dict(zip(queries, _SEARCH_POOL.map(_search, queries)))

    # What each board's pins matched, for judging its theme's phrasings.
    board_cats: dict[str, set] = defaultdict(set)
    for seed in seeds:
        items = results[seed.queries[0]]
        if seed.kind == "pin" and items and items[0].score >= MIN_MATCH_SCORE:
            board_cats[seed.group] |= {i.data.get("categories") for i in items[:3]}

    history = []
    matched: list[tuple[Seed, str, list[Item]]] = []
    unmatched: list[str] = []
    for seed in seeds:
        query, score = best_query(seed, results, board_cats[seed.group])
        hits = [i for i in results[query] if within_budget(i, max_price)]
        if score < MIN_MATCH_SCORE or not hits:
            if seed.kind == "interest":
                unmatched.append(seed.label)
            continue
        matched.append((seed, query, hits))
    for _, query, hits in reversed(matched):
        history += [Search(query), View(hits[0].id)]
    return history, matched, unmatched


def to_card(item: Item, reason: str, group: str | None = None) -> dict:
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
        "group": group,
    }


def closest_group(item: Item, matched) -> str | None:
    """Which interest or board a blended pick belongs to: among the seeds whose
    own search results include its category, the one sharing the most words
    with it ("Rolex: 3,621 Wristwatches" goes with the watch board), so that
    board doesn't get a second card. None if nothing is close."""
    words = _words(item.data.get("name", ""))
    category = item.data.get("categories")
    best, best_overlap = None, 0
    for seed, query, _ in matched:
        hits = _search(query)
        if any(h.id == item.id for h in hits):
            return seed.group
        if category not in {h.data.get("categories") for h in hits}:
            continue
        overlap = len(words & set().union(*(_words(h.data.get("name", "")) for h in hits[:5])))
        if overlap > best_overlap:
            best, best_overlap = seed.group, overlap
    return best


def pick_gifts(seeds: list[Seed], req: GiftRequest):
    # Seeds come in priority order (for pins, already mixed across boards).
    # Groups shown recently move back and a focused group to the front. That
    # order also sets the history, whose latest events steer the model most,
    # so the top match drifts as they ask for others.
    recent = set(req.recent_groups)
    seeds = sorted(seeds, key=lambda seed: (seed.group != req.focus, seed.group in recent))
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
    used_names: list[set[str]] = [name_words(n) for n in req.avoid_similar]

    def take(item: Item, reason: str, group: str | None = None) -> bool:
        cat = category_key(item)
        words = name_words(item.data.get("name", ""))
        if item.id in used_ids or item.id in seen_ids or cat in used_cats:
            return False
        if any(near_copy(words, used) for used in used_names):
            return False
        if not within_budget(item, req.max_price) or not fits_age(item, req.age):
            return False
        picks.append(to_card(item, reason, group))
        used_ids.add(item.id)
        used_cats.add(cat)
        used_names.append(words)
        return True

    def personalized(query: str, limit: int = 15) -> list[Item]:
        return get_client().complete(history=[*history, Search(query)], limit=limit).products.items

    # The model's view of "what does this person want next", given everything.
    recs = get_client().complete(history=history, limit=40).products.items

    groups_used: set[str] = set()
    if not any(m[0].group == req.focus for m in matched):
        # 1) Best blended pick: the top recommendation across all interests.
        #    It counts as the card for whichever interest or board it's from.
        #    Like the other cards it has to be on topic: from a category that
        #    one of their interests or themes returns (pins only if that's all
        #    there is), which keeps out stray carpet grippers and hummus.
        lead = "More like what you saved" if req.liked else "Top match for their whole profile"
        topical = [m for m in matched if m[0].kind != "pin"] or matched
        on_topic = {i.data.get("categories") for _, query, _ in topical for i in _search(query)}
        for item in gifts_first(i for i in recs if i.data.get("categories") in on_topic):
            if take(item, lead):
                group = closest_group(item, matched)
                picks[-1]["group"] = group
                groups_used.add(group)
                break

    # 2) Then one per interest or board, personalized by the full profile, so
    #    the alternatives cover different things they love. A board's card
    #    comes from its theme when Pinterest gave topics for it, else from a
    #    pin. With an occasion or age set, the search says so ("fixie bike
    #    birthday gift"). Pins can't be phrased that way, so if a pin has to
    #    fill a card and an occasion is picked, the last slot searches the
    #    occasion alone, still personalized by the profile.
    #    Each card stays on its topic: only products from categories its own
    #    plain search returned, so a bike board can't drift into food just
    #    because the profile has a big food board. A few spare groups are
    #    searched in case some only repeat earlier picks.
    candidates = []
    for m in matched:
        if m[0].group not in groups_used and all(c[0].group != m[0].group for c in candidates):
            candidates.append(m)
    candidates = candidates[: n + 3]
    occasion_slot = bool(req.occasion) and any(seed.kind == "pin" for seed, _, _ in candidates)
    seed_slots = n - 1 if occasion_slot and n > 1 else n

    def phrased(seed: Seed, query: str) -> str:
        topic = topic_of(seed, query)
        return f"{topic} {ctx}" if ctx and topic else query

    with ThreadPoolExecutor(max_workers=len(candidates) + 1) as pool:
        per_seed = pool.map(lambda c: personalized(phrased(c[0], c[1])), candidates)
        for_occasion = pool.submit(personalized, ctx) if occasion_slot else None
        for (seed, query, hits), items in zip(candidates, per_seed):
            if len(picks) >= seed_slots:
                break
            on_topic = {i.data.get("categories") for i in _search(query)}
            for item in gifts_first([*(i for i in items if i.data.get("categories") in on_topic), *hits]):
                if take(item, reason_for(seed, query), seed.group):
                    break
        if for_occasion:
            for item in gifts_first(for_occasion.result()):
                if len(picks) >= n or take(item, f"A {ctx}"):
                    break

    # 3) Fill any remaining slots from the blended list, then the raw seeds.
    for item in gifts_first(recs):
        if len(picks) >= n:
            break
        take(item, "Also fits their profile")
    for seed, query, hits in matched:
        for item in gifts_first(hits):
            if len(picks) >= n:
                break
            take(item, reason_for(seed, query), seed.group)

    # 4) Still short, usually because nothing they like fits the age set:
    #    search the occasion or age alone, personalized by the profile.
    general = f"A {ctx}"
    if len(picks) < n and ctx:
        for item in gifts_first(personalized(ctx, limit=30)):
            if len(picks) >= n:
                break
            take(item, general)
    note = None
    if req.age in _CHILD_CATEGORIES and picks and all(p["reason"] == general for p in picks):
        note = f"Nothing in their {'interests' if matched[0][0].kind == 'interest' else 'pins'} suits a {_AGE_TERMS[req.age].rstrip('s')}, so these are general ideas."

    return picks[:n], unmatched, note


_pin_cache: dict[str, tuple[float, tuple]] = {}


def cached_profile(link: str):
    """Refining picks re-sends the same link, so keep its pins for a while."""
    key = link.strip().lower()
    hit = _pin_cache.get(key)
    if hit and time.monotonic() - hit[0] < PIN_CACHE_SECONDS:
        return hit[1]
    result = fetch_profile(link)
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
            source, pins, boards = cached_profile(req.pinterest)
        except PinterestError as exc:
            raise HTTPException(400, str(exc)) from exc
        pinterest = {"label": source.label, "url": source.page_url, "pins": len(pins), "boards": len(boards)}
        # Themes first: they lead the history, mixed one per board.
        seeds += [theme_seed(b) for b in boards if b.topics] + [pin_seed(p) for p in pins]
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


# The pages hold all the front-end code, so browsers should check for a new
# version on every load instead of running a stale copy against a newer API.
NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers=NO_CACHE)


@app.get("/list/{list_id}")
def list_page(list_id: str):
    return FileResponse(STATIC / "list.html", headers=NO_CACHE)
