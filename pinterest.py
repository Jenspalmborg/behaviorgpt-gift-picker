"""Read someone's pins from a public Pinterest profile or board.

Pinterest publishes an RSS feed for every public profile (/<user>/feed.rss)
and board (/<user>/<board>.rss), so no login or API key is needed. For a
profile, pins are drawn from each board in proportion to its size, so the
board they happened to pin to last week doesn't drown out the rest.
"""

import html
import json
import math
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

MAX_PINS = 16  # from a whole profile, spread over its boards
BOARD_PINS = 12  # from a single board link
MAX_QUERY_WORDS = 25

_HEADERS = {"User-Agent": "Mozilla/5.0 (GiftPicker)"}
_PINTEREST_HOST = re.compile(r"(^|\.)pinterest\.[a-z]{2,3}(\.[a-z]{2})?$")
_SHORT_HOSTS = {"pin.it", "api.pinterest.com"}
_USER = re.compile(r"^[A-Za-z0-9_]{3,30}$")
_BOARD = re.compile(r"^[\w-]{1,100}$")
# Profile tabs and site pages that look like /<user>/<board>/ but aren't boards.
_PROFILE_TABS = {"_saved", "_created", "_shop", "boards", "pins"}
_NOT_USERS = {"pin", "search", "ideas", "today", "explore", "business", "settings", "_"}


class PinterestError(ValueError):
    """Shown to the user as-is."""


@dataclass(frozen=True)
class Pin:
    text: str
    board: str = ""  # board name; "" when it came from the profile's recent feed


@dataclass
class Source:
    user: str
    board: str | None = None

    @property
    def feed_url(self) -> str:
        path = f"{self.user}/{self.board}.rss" if self.board else f"{self.user}/feed.rss"
        return f"https://www.pinterest.com/{path}"

    @property
    def page_url(self) -> str:
        path = f"{self.user}/{self.board}/" if self.board else f"{self.user}/"
        return f"https://www.pinterest.com/{path}"

    @property
    def label(self) -> str:
        return f"{self.user}/{self.board}" if self.board else self.user


def looks_like_pinterest(text: str) -> bool:
    return bool(re.search(r"pinterest\.|pin\.it/", text, re.I))


def _resolve_short_link(url: str, client: httpx.Client) -> str:
    """pin.it links redirect a few times before reaching pinterest.com. Follow
    them by hand so we never fetch a host outside Pinterest."""
    for _ in range(5):
        host = urlparse(url).hostname or ""
        if host not in _SHORT_HOSTS:
            return url
        resp = client.get(url, follow_redirects=False)
        location = resp.headers.get("location")
        if not resp.is_redirect or not location:
            break
        url = urljoin(url, location)
    raise PinterestError("Couldn't open that pin.it link. Paste the full profile or board link instead.")


def parse_source(text: str, client: httpx.Client) -> Source:
    """Accepts a profile or board URL, a pin.it short link, a username, or "user/board"."""
    text = text.strip()
    if _USER.match(text.removeprefix("@")):
        return Source(text.removeprefix("@"))
    # "jane/cozy-home", as used in shareable links
    user, _, board = text.removeprefix("@").strip("/").partition("/")
    if _USER.match(user) and _BOARD.match(board):
        return Source(user, None if board.lower() in _PROFILE_TABS else board)
    if not re.match(r"^https?://", text, re.I):
        text = "https://" + text

    if (urlparse(text).hostname or "").lower() in _SHORT_HOSTS:
        text = _resolve_short_link(text, client)

    parsed = urlparse(text)
    if not _PINTEREST_HOST.search((parsed.hostname or "").lower()):
        raise PinterestError("That doesn't look like a Pinterest link.")
    parts = [p for p in parsed.path.split("/") if p]
    if not parts or parts[0].lower() in _NOT_USERS:
        if parts and parts[0].lower() == "pin":
            raise PinterestError("That's a single pin. Paste a link to their profile or one of their boards.")
        raise PinterestError("Paste a link to a Pinterest profile or board.")
    user = parts[0]
    if not _USER.match(user):
        raise PinterestError("That doesn't look like a Pinterest profile link.")
    board = parts[1] if len(parts) > 1 and parts[1].lower() not in _PROFILE_TABS else None
    if board and not _BOARD.match(board):
        board = None
    return Source(user, board)


# Pins saved straight from an image keep its file name as their title:
# "enhanced-buzz-31045-1434731585-7.jpg 600 × 2 557 pixlar".
_FILE_NAME = re.compile(r"\S*\.(?:jpe?g|png|gif|webp)\b.*$", re.I)


def _clean(text: str) -> str:
    text = html.unescape(text).replace("[Video]", " ")
    text = _FILE_NAME.sub("", text).replace("_", " ")
    return " ".join(text.split()[:MAX_QUERY_WORDS])


def _pin_text(item: ET.Element) -> str:
    """Pin title, falling back to the description with its <img> markup removed."""
    text = item.findtext("title") or ""
    if not text.strip():
        text = re.sub(r"<[^>]+>", " ", html.unescape(item.findtext("description") or ""))
    return _clean(text)


# Untitled pins get a page title like "Pin on Jane" / "Pin på Jane" / "Pin von Jane".
_GENERIC_TITLE = re.compile(r"^(pin|pinterest)\b(\s+\w+\s+\S+)?$", re.I)


def _page_title(url: str, client: httpx.Client) -> str:
    """Many people save pins without a caption, so the feed has no text for
    them. The pin's own page still has a title built from Pinterest's topic
    tags, like "Diy dinosaur play house | Dinosaur dollhouse, Dino house"."""
    parsed = urlparse(url)
    if not _PINTEREST_HOST.search((parsed.hostname or "").lower()) or not re.match(r"^/pin/\d+/?$", parsed.path):
        return ""
    try:
        resp = client.get(f"https://www.pinterest.com{parsed.path}", follow_redirects=True)
    except httpx.HTTPError:
        return ""
    m = re.search(r"<title[^>]*>([^<]*)</title>", resp.text) if resp.status_code == 200 else None
    title = _clean(m.group(1)) if m else ""
    return "" if _GENERIC_TITLE.match(title) else title


def _profile_boards(user: str, client: httpx.Client) -> dict[str, tuple[str, int]]:
    """{slug: (name, pin count)} for the user's public boards, read from the
    data embedded in their profile page. Empty if the page can't be read."""
    try:
        resp = client.get(f"https://www.pinterest.com/{user}/", follow_redirects=True)
    except httpx.HTTPError:
        return {}
    data = []
    for m in re.finditer(r'<script id="__PWS_(?:INITIAL_PROPS|DATA)__" type="application/json">(.*?)</script>', resp.text, re.S):
        try:
            data.append(json.loads(m.group(1)))
        except ValueError:
            pass

    boards: dict[str, tuple[str, int]] = {}
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            stack.extend(node.values())
            parts = str(node.get("url") or "").strip("/").split("/")
            count = node.get("pin_count")
            if (node.get("type") == "board" and len(parts) == 2 and parts[0].lower() == user.lower()
                    and _BOARD.match(parts[1]) and node.get("privacy", "public") == "public"
                    and isinstance(count, int) and count > 0):
                boards[parts[1]] = (str(node.get("name") or parts[1]), count)
    return boards


def allocate(sizes: dict[str, int], budget: int) -> dict[str, int]:
    """Split `budget` pins over boards: one each, the rest by the square root
    of board size. 1,000 watches vs 10 clothes comes out about 7:1, so the
    big board leads without silencing the small one."""
    boards = sorted(sizes, key=lambda b: -sizes[b])[:budget]
    if not boards:
        return {}
    shares = dict.fromkeys(boards, 1)
    spare = budget - len(boards)
    weight = {b: math.sqrt(sizes[b]) for b in boards}
    ideal = {b: spare * weight[b] / sum(weight.values()) for b in boards}
    for b in boards:
        shares[b] += int(ideal[b])
    leftover = budget - sum(shares.values())
    for b in sorted(boards, key=lambda b: int(ideal[b]) - ideal[b])[:leftover]:
        shares[b] += 1
    return {b: min(n, sizes[b]) for b, n in shares.items()}


def _feed_items(url: str, client: httpx.Client, missing: str) -> list[ET.Element]:
    try:
        resp = client.get(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise PinterestError("Couldn't reach Pinterest. Try again in a moment.") from exc
    if resp.status_code == 404:
        raise PinterestError(missing)
    if resp.status_code != 200 or "xml" not in resp.headers.get("content-type", ""):
        raise PinterestError("Pinterest didn't return their pins. Try again in a moment.")
    try:
        return list(ET.fromstring(resp.content).iter("item"))
    except ET.ParseError as exc:
        raise PinterestError("Pinterest sent back something unreadable. Try again.") from exc


def _titles(items: list[ET.Element], client: httpx.Client) -> list[str]:
    """Pin texts in feed order, using the pin's page title where the feed has none."""
    texts = [_pin_text(item) for item in items]
    untitled = [i for i, t in enumerate(texts) if not t]
    if untitled:
        with ThreadPoolExecutor(max_workers=min(len(untitled), 16)) as pool:
            links = [items[i].findtext("link") or "" for i in untitled]
            for i, title in zip(untitled, pool.map(lambda u: _page_title(u, client), links)):
                texts[i] = title
    return texts


def _interleave(per_board: list[list[Pin]]) -> list[Pin]:
    """Round-robin over boards (biggest first), so any prefix is a mix."""
    out = []
    for i in range(max(map(len, per_board), default=0)):
        out += [pins[i] for pins in per_board if i < len(pins)]
    return out


def fetch_pins(text: str) -> tuple[Source, list[Pin]]:
    """Returns the source and its pins, most important first: for a profile,
    a size-weighted mix of its boards; for a board, its newest pins."""
    with httpx.Client(headers=_HEADERS, timeout=10) as client:
        source = parse_source(text, client)
        boards = {} if source.board else _profile_boards(source.user, client)

        if boards:
            shares = allocate({slug: count for slug, (_, count) in boards.items()}, MAX_PINS)

            def board_pins(slug: str) -> list[Pin]:
                try:
                    items = _feed_items(Source(source.user, slug).feed_url, client, "")
                except PinterestError:
                    return []
                # Fetch a couple extra in case some have no readable title.
                texts = [t for t in _titles(items[: shares[slug] + 2], client) if t]
                return [Pin(t, boards[slug][0]) for t in texts[: shares[slug]]]

            with ThreadPoolExecutor(max_workers=len(shares)) as pool:
                pins = _interleave([p for p in pool.map(board_pins, shares) if p])
        else:
            what = "board" if source.board else "profile"
            items = _feed_items(source.feed_url, client, f"Couldn't find that Pinterest {what}. Is it public?")
            if not items:
                raise PinterestError("That Pinterest profile or board has no public pins yet.")
            pins = [Pin(t, source.board or "") for t in _titles(items[:BOARD_PINS], client) if t]

    pins = list({p.text: p for p in reversed(pins)}.values())[::-1]  # dedupe, keep first
    if not pins:
        raise PinterestError("Their pins don't have any titles or descriptions we can read. Try one of their boards, or type their interests instead.")
    return source, pins


def short_label(pin: str, words: int = 6) -> str:
    """'You can grow a kumquat tree at home for…' → 'You can grow a kumquat tree…'
    and 'Diy dinosaur play house | Dinosaur dollhouse, …' → 'Diy dinosaur play house'"""
    w = pin.split(" | ")[0].rstrip(" .!").split()
    return " ".join(w[:words]) + ("…" if len(w) > words else "")
