import time

import pytest

from app import bot, config, feeds, images, net, threads

RSS = """<?xml version="1.0"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>t</title>
<item><title>Delhi Metro adds new line</title><link>https://www.thehindu.com/news/a1/?utm_source=rss</link>
<description>&lt;p&gt;The Delhi Metro will open a new line.&lt;/p&gt;</description><pubDate>{now}</pubDate>
<media:content url="https://img.example/a1.jpg" medium="image"/></item>
<item><title>Apple launches iPhone in the US</title><link>https://example.com/news/a2</link>
<description>Apple announced a phone for American buyers.</description><pubDate>{now}</pubDate></item>
<item><title>Old story</title><link>https://example.com/news/a3</link><description>x</description>
<pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item></channel></rss>"""


def rss_now() -> str:
    return RSS.replace("{now}", time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()))


# ---- India keyword filter and feed parsing -----------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Goal scored in the final minute", False), ("Goa tourism hits a record", True), ("Tata Motors launches new EV", True),
    ("Price cut to ₹999 for the phone", True), ("Rs 5,000 crore plan", True), ("Olaf Scholz visits Berlin", False),
    ("India beat Australia", True), ("Apple launches iPhone in US", False), ("Sensex falls 300 points", True),
    ("Ola Electric shares jump", True),
])
def test_india_keyword_filter(text, expected):
    assert bool(feeds.INDIA_RE.search(text)) is expected


def test_feed_items_are_normalised_deduplicated_and_aged(monkeypatch, fake_response):
    monkeypatch.setattr(net, "get", lambda *a, **k: fake_response(rss_now()))
    items = feeds._fetch_feed("india", "The Hindu", "https://feed.example/rss")
    by_title = {i["title"]: i for i in items}
    first = by_title["Delhi Metro adds new line"]
    assert first["url"] == "https://www.thehindu.com/news/a1"         # query string and trailing slash removed
    assert first["summary"] == "The Delhi Metro will open a new line."  # HTML stripped
    assert first["image"] == "https://img.example/a1.jpg"
    assert first["source"] == "The Hindu" and first["category"] == "india"


def test_mixed_feeds_keep_only_indian_stories(monkeypatch, fake_response):
    monkeypatch.setattr(net, "get", lambda *a, **k: fake_response(rss_now()))
    titles = [i["title"] for i in feeds._fetch_feed("technology", "Gadgets", "u", mixed=True)]
    assert "Delhi Metro adds new line" in titles and "Apple launches iPhone in the US" not in titles


def test_a_broken_feed_returns_nothing_instead_of_crashing(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("down")
    monkeypatch.setattr(net, "get", boom)
    assert feeds._fetch_feed("india", "X", "u") == []


# ---- images ----------------------------------------------------------------------------------------

def test_sensitive_and_political_stories_never_get_stock_photos():
    assert images._stock_ok({"category": "environment", "title": "Solar installations hit a record"}, "")
    assert not images._stock_ok({"category": "politics", "title": "Budget"}, "")
    assert not images._stock_ok({"category": "india", "title": "Governor sentenced over fraud"}, "")


ITEM = {"url": "https://thehindu.com/x/1", "title": "Solar installations hit a record", "category": "environment",
        "source": "The Hindu", "summary": "s", "image": ""}


def test_article_image_comes_first_with_a_publisher_credit(monkeypatch):
    monkeypatch.setattr(images, "find_article_image", lambda item: "https://th-i.thgim.com/p.jpg")
    assert images.pick_image(ITEM, "post", "solar panels") == ("https://th-i.thgim.com/p.jpg", "article", "Image: The Hindu")


def test_stock_photo_is_used_when_the_article_has_no_image(monkeypatch):
    monkeypatch.setattr(images, "find_article_image", lambda item: None)
    monkeypatch.setattr(images, "stock_available", lambda: True)
    monkeypatch.setattr(images, "find_stock_image", lambda i, p, q: ("https://pexels/p.jpg", None))
    assert images.pick_image(ITEM, "post", "solar panels") == ("https://pexels/p.jpg", "stock", None)   # no credit


def test_text_only_when_nothing_fits(monkeypatch):
    monkeypatch.setattr(images, "find_article_image", lambda item: None)
    monkeypatch.setattr(images, "stock_available", lambda: True)
    monkeypatch.setattr(images, "find_stock_image", lambda i, p, q: None)
    assert images.pick_image(ITEM, "post", "x") == (None, None, None)


@pytest.mark.parametrize("mode,expected_kind", [("stock", "stock"), ("article", "article"), ("off", None)])
def test_image_modes(monkeypatch, mode, expected_kind):
    monkeypatch.setattr(config, "IMAGE_MODE", mode)
    monkeypatch.setattr(images, "find_article_image", lambda item: "https://a/p.jpg")
    monkeypatch.setattr(images, "stock_available", lambda: True)
    monkeypatch.setattr(images, "find_stock_image", lambda i, p, q: ("https://s/p.jpg", None))
    assert images.pick_image(ITEM, "post", "q")[1] == expected_kind


def test_stock_photo_needs_a_search_phrase_and_a_clear_match(monkeypatch):
    monkeypatch.setattr(config, "PEXELS_API_KEY", "k")
    monkeypatch.setattr(images, "STOCK", {"pexels": (lambda: "k", lambda q: [
        {"url": "https://p/1.jpg", "desc": "cat on a sofa", "credit": "A / Pexels"}])})
    monkeypatch.setattr(config, "STOCK_PROVIDERS", ["pexels"])
    monkeypatch.setattr(images.llm, "judge_photo", lambda item, post, cands: None)   # the judge says none fits
    assert images.find_stock_image(ITEM, "post", "solar panels") is None
    assert images.find_stock_image(ITEM, "post", "") is None                          # no phrase, no search
    monkeypatch.setattr(images.llm, "judge_photo", lambda item, post, cands: 0)
    monkeypatch.setattr(images, "_usable", lambda url: True)
    assert images.find_stock_image(ITEM, "post", "solar panels") == ("https://p/1.jpg", None)   # never credited


# ---- publish fallback chain --------------------------------------------------------------------------

DRAFT = {"url": "https://thehindu.com/x/1", "title": "Solar record", "category": "environment", "source": "The Hindu",
         "post": "Solar panel installations hit a record.", "tags": ["Environment", "Solar"], "photo_query": "solar panels",
         "image_url": "https://th-i.thgim.com/p.jpg", "image": "article", "credit": "Image: The Hindu"}


def test_rejected_article_image_falls_back_to_a_stock_photo_then_text(monkeypatch):
    tried = []

    def publish(text, link=None, image_url=None):
        tried.append(image_url or "TEXT")
        if image_url and "thgim" in image_url:
            raise RuntimeError("Threads rejected the image")
        return "id1"

    monkeypatch.setattr(threads, "publish", publish)
    monkeypatch.setattr(images, "stock_available", lambda: True)
    monkeypatch.setattr(images, "find_stock_image", lambda i, p, q: ("https://pexels/s.jpg", None))
    out = bot.publish_draft(DRAFT)
    assert tried == ["https://th-i.thgim.com/p.jpg", "https://pexels/s.jpg"] and out["image"] == "stock"
    assert "Source: The Hindu" in out["text"] and DRAFT["url"] in out["text"]       # source instead of a credit
    assert "Pexels" not in out["text"] and "\U0001F4F7" not in out["text"] and "Image:" not in out["text"]

    tried.clear()
    monkeypatch.setattr(images, "find_stock_image", lambda i, p, q: None)
    out = bot.publish_draft(DRAFT)
    assert tried == ["https://th-i.thgim.com/p.jpg", "TEXT"] and out["image"] is None


def test_if_even_a_text_post_fails_the_error_is_raised(monkeypatch):
    def publish(text, link=None, image_url=None):
        raise RuntimeError("token expired")
    monkeypatch.setattr(threads, "publish", publish)
    with pytest.raises(RuntimeError, match="token expired"):
        bot.publish_draft(dict(DRAFT, image_url=None, image=None))
