import json

import httpx
import pytest

import pinterest
from pinterest import Pin, PinterestError, _clean, _interleave, allocate, fetch_profile, parse_source, short_label


@pytest.mark.parametrize("text, feed", [
    ("@jane", "https://www.pinterest.com/jane/feed.rss"),
    ("jane", "https://www.pinterest.com/jane/feed.rss"),
    ("jane/cozy-home", "https://www.pinterest.com/jane/cozy-home.rss"),
    ("pinterest.com/jane/", "https://www.pinterest.com/jane/feed.rss"),
    ("https://se.pinterest.com/jane/", "https://www.pinterest.com/jane/feed.rss"),
    ("https://www.pinterest.co.uk/jane/cozy-home/", "https://www.pinterest.com/jane/cozy-home.rss"),
    ("https://www.pinterest.com/jane/_saved/", "https://www.pinterest.com/jane/feed.rss"),
])
def test_parse_source(text, feed):
    assert parse_source(text, client=None).feed_url == feed


@pytest.mark.parametrize("text, message", [
    ("https://www.pinterest.com/pin/123/", "single pin"),
    ("https://evil.com/jane", "doesn't look like a Pinterest link"),
    ("https://pinterest.com.evil.com/jane", "doesn't look like a Pinterest link"),
    ("https://www.pinterest.com/", "profile or board"),
])
def test_parse_source_rejects(text, message):
    with pytest.raises(PinterestError, match=message):
        parse_source(text, client=None)


def test_allocate_weights_big_boards_but_keeps_small_ones():
    shares = allocate({"watches": 15, "art": 13, "mat": 4, "prag": 4, "prylar": 3, "vegetation": 2, "inspo": 1}, 16)
    assert all(n >= 1 for n in shares.values())
    assert shares["watches"] >= shares["art"] > shares["inspo"]
    assert sum(shares.values()) <= 16
    lopsided = allocate({"watches": 1000, "clothes": 10}, 16)
    assert lopsided["watches"] > 4 * lopsided["clothes"] and lopsided["clothes"] >= 1
    assert allocate({"a": 2}, 16) == {"a": 2}  # never more than the board holds
    assert len(allocate({f"b{i}": 5 for i in range(20)}, 16)) == 16


def test_interleave_mixes_boards():
    watches = [Pin(f"w{i}", "Watches") for i in range(3)]
    art = [Pin("a0", "Art")]
    assert [p.text for p in _interleave([watches, art])] == ["w0", "a0", "w1", "w2"]


def test_clean_drops_file_names():
    assert _clean("enhanced-buzz-31045-7.jpg 600 × 2 557 pixlar") == ""
    assert _clean("harvest_single-speed_bike") == "harvest single-speed bike"
    assert _clean("Diy play house [Video] | Dino") == "Diy play house | Dino"


def test_short_label():
    assert short_label("You can grow a kumquat tree at home for sweet citrus") == "You can grow a kumquat tree…"
    assert short_label("Diy dinosaur play house | Dinosaur dollhouse, Dino house") == "Diy dinosaur play house"


FEED = """<?xml version="1.0"?><rss version="2.0"><channel><title>Jane</title>
<item><title>Trail running vest for long runs</title><link>https://www.pinterest.com/pin/111/</link><description>x</description></item>
<item><title> </title><link>https://se.pinterest.com/pin/222/</link>
  <description>&lt;a href="https://se.pinterest.com/pin/222/"&gt;&lt;img src="https://i.pinimg.com/a.jpg"&gt;&lt;/a&gt; </description></item>
<item><title> </title><link>https://www.pinterest.com/pin/333/</link><description> </description></item>
<item><title> </title><link>https://evil.com/pin/444/</link><description> </description></item>
</channel></rss>"""


@pytest.fixture
def fake_pinterest(monkeypatch):
    """Serves the feed above; pin 222's page has a title, 333's is generic."""
    fetched = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        path = request.url.path
        if path == "/jane/feed.rss":
            return httpx.Response(200, text=FEED, headers={"content-type": "text/xml"})
        if path == "/pin/222/":
            return httpx.Response(200, text="<title>Diy dinosaur play house | Dino house</title>")
        if path == "/pin/333/":
            return httpx.Response(200, text="<title>Pin on Jane</title>")
        return httpx.Response(404)

    real_client = httpx.Client
    monkeypatch.setattr(pinterest.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    return fetched


def test_fetch_pins_falls_back_to_the_pin_page_title(fake_pinterest):
    source, pins, boards = fetch_profile("https://se.pinterest.com/jane/")
    assert source.label == "jane"
    assert [p.text for p in pins] == ["Trail running vest for long runs", "Diy dinosaur play house | Dino house"]
    # Pin pages are only fetched from Pinterest, never from a link in the feed.
    assert not any("evil.com" in url for url in fake_pinterest)


def test_fetch_pins_reports_a_missing_profile(fake_pinterest):
    with pytest.raises(PinterestError, match="Is it public"):
        fetch_profile("nobody")


def board_feed(*titles):
    items = "".join(f"<item><title>{t}</title><link>https://www.pinterest.com/pin/{i}/</link></item>" for i, t in enumerate(titles))
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>'


@pytest.fixture
def fake_profile(monkeypatch):
    """A profile with a big watch board, a small art board and a private one."""
    boards = [
        {"type": "board", "name": "Watches", "url": "/jane/watches/", "pin_count": 40, "privacy": "public"},
        {"type": "board", "name": "Art", "url": "/jane/art/", "pin_count": 3, "privacy": "public"},
        {"type": "board", "name": "Secret", "url": "/jane/secret/", "pin_count": 9, "privacy": "secret"},
    ]
    page = f'<script id="__PWS_INITIAL_PROPS__" type="application/json">{json.dumps({"x": {"boards": boards}})}</script>'
    ideas = lambda *keys: ('<script id="__PWS_DATA__" type="application/json">'
                           + json.dumps({"related": [{"url": f"/ideas/{k.replace(' ', '-')}/1/", "key": k} for k in keys]})
                           + "</script>")
    pages = {"/jane/": page, "/jane/watches/": ideas("luxury watches", "vintage watches"),
             "/jane/art/": ideas("art", "sea sculpture", "lovecraft art", "weird vintage", "josef sudek", "dada")}
    feeds = {
        "/jane/watches.rss": board_feed(*[f"Watch {i}" for i in range(20)]),
        "/jane/art.rss": board_feed("Sculpture", "Painting", "Print"),
    }

    def handler(request):
        if request.url.path in pages:
            return httpx.Response(200, text=pages[request.url.path])
        if request.url.path in feeds:
            return httpx.Response(200, text=feeds[request.url.path], headers={"content-type": "text/xml"})
        return httpx.Response(404)

    real_client = httpx.Client
    monkeypatch.setattr(pinterest.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))


def test_profile_pins_are_drawn_from_every_public_board(fake_profile):
    _, pins, boards = fetch_profile("jane")
    boards = [p.board for p in pins]
    assert set(boards) == {"Watches", "Art"}  # the secret board is skipped
    assert boards.count("Watches") > boards.count("Art") >= 2
    assert boards[:2] == ["Watches", "Art"]  # mixed, biggest board first



def test_boards_come_with_pinterests_topics(fake_profile):
    _, _, boards = fetch_profile("jane")
    assert [(b.name, b.size) for b in boards] == [("Watches", 40), ("Art", 3)]
    assert boards[0].topics == ("luxury watches", "vintage watches")
    assert boards[1].topics == ("art", "sea sculpture", "lovecraft art", "weird vintage", "josef sudek")  # capped at five
