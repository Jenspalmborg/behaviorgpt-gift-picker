import httpx
import pytest

import pinterest
from pinterest import PinterestError, fetch_pins, parse_source, short_label


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
    source, pins = fetch_pins("https://se.pinterest.com/jane/")
    assert source.label == "jane"
    assert pins == ["Trail running vest for long runs", "Diy dinosaur play house | Dino house"]
    # Pin pages are only fetched from Pinterest, never from a link in the feed.
    assert not any("evil.com" in url for url in fake_pinterest)


def test_fetch_pins_reports_a_missing_profile(fake_pinterest):
    with pytest.raises(PinterestError, match="Is it public"):
        fetch_pins("nobody")
