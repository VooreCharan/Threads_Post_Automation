import json

from app import article, net

LONG_PARAS = [f"Paragraph {i}: the municipal council announced a detailed plan covering roads, water supply and "
              f"public transport for the coming year, officials said on Thursday." for i in range(1, 6)]


def page(body_html: str, head: str = "") -> str:
    return f"<html><head>{head}</head><body>{body_html}</body></html>"


# ---- cleaning ----------------------------------------------------------------------------------------

def test_polish_removes_related_headlines_and_app_promos():
    dirty = ("The index closed higher.Also Read | In 2000, the S&P 500 crashed 49%; now most stocks are down. "
             "Among the 30 constituents, banks led. Get the latest India News and Live updates. Download the TOI app.")
    clean = article._polish(dirty)
    assert "Also Read" not in clean and "Download the" not in clean and "Get the latest" not in clean
    assert "S&P 500" not in clean                      # the related-article headline is gone
    assert clean == "The index closed higher. Among the 30 constituents, banks led."


def test_polish_repairs_missing_space_after_a_full_stop():
    assert article._polish("operated on Thursday.The service starts (DoT).Calls follow.") == \
        "operated on Thursday. The service starts (DoT). Calls follow."


def test_polish_keeps_abbreviations_intact():
    text = "The U.S. Open and B.M. Road were mentioned. Dr. Rao spoke."
    assert article._polish(text) == text


# ---- extraction --------------------------------------------------------------------------------------

def test_jsonld_article_body_is_used_first():
    ld = json.dumps({"@context": "https://schema.org", "@type": "NewsArticle",
                     "articleBody": "<p>" + "</p><p>".join(LONG_PARAS) + "</p>"})
    html = page("<p>nav junk</p>", head=f'<script type="application/ld+json">{ld}</script>')
    soup = article.BeautifulSoup(html, "html.parser")
    body = article._jsonld_body(soup)
    assert LONG_PARAS[0] in body and "nav junk" not in body


def test_jsonld_inside_a_graph():
    ld = json.dumps({"@graph": [{"@type": "WebSite"}, {"@type": "NewsArticle", "articleBody": " ".join(LONG_PARAS)}]})
    soup = article.BeautifulSoup(page("", head=f'<script type="application/ld+json">{ld}</script>'), "html.parser")
    assert LONG_PARAS[2] in article._jsonld_body(soup)


def test_html_body_drops_navigation_ads_and_related_links():
    html = page(
        "<nav><p>Home | India | World | Sports | Business | Entertainment | Technology | Health</p></nav>"
        "<article>" + "".join(f"<p>{p}</p>" for p in LONG_PARAS[:3]) +
        "<p>Also Read | A completely unrelated headline that links to another story page</p>"
        "<div class='related-stories'><p>Another related story headline that is long enough to be a paragraph</p></div>"
        "<aside><p>Sponsored content about something you should buy right now, limited time offer!!!</p></aside>"
        "<p>Advertisement: please subscribe to our newsletter for daily updates and offers.</p>"
        "</article><footer><p>Copyright 2026 all rights reserved by the publisher of this website</p></footer>")
    soup = article.BeautifulSoup(html, "html.parser")
    body = article._html_body(soup)
    assert LONG_PARAS[0] in body and LONG_PARAS[2] in body
    for junk in ("Home | India", "Also Read", "Another related", "Sponsored", "Advertisement", "Copyright"):
        assert junk not in body


# ---- fetch: fallbacks and caching ------------------------------------------------------------------

def test_fetch_uses_jsonld_when_present(monkeypatch, fake_response):
    ld = json.dumps({"@type": "NewsArticle", "articleBody": " ".join(LONG_PARAS)})
    monkeypatch.setattr(net, "get", lambda *a, **k: fake_response(page("", head=f'<script type="application/ld+json">{ld}</script>')))
    text, how = article.fetch("https://x.in/a", "short summary")
    assert how == "jsonld" and LONG_PARAS[0] in text


def test_fetch_falls_back_to_the_summary_when_the_site_blocks_us(monkeypatch, fake_response):
    monkeypatch.setattr(net, "get", lambda *a, **k: fake_response("denied", status=403))
    text, how = article.fetch("https://blocked.example/a", "A one line summary.")
    assert (text, how) == ("A one line summary.", "summary")


def test_fetch_survives_network_errors(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("down")
    monkeypatch.setattr(net, "get", boom)
    assert article.fetch("https://x.in/b", "Summary text.") == ("Summary text.", "summary")


def test_fetch_is_cached(monkeypatch, fake_response):
    calls = []

    def fake_get(*a, **k):
        calls.append(1)
        return fake_response(page("<article>" + "".join(f"<p>{p}</p>" for p in LONG_PARAS) + "</article>"))
    monkeypatch.setattr(net, "get", fake_get)
    article.fetch("https://x.in/c", "")
    article.fetch("https://x.in/c", "")
    assert len(calls) == 1


def test_text_is_trimmed_to_the_limit(monkeypatch, fake_response):
    ld = json.dumps({"@type": "NewsArticle", "articleBody": "\n".join("Sentence number %d is here and fine." % i * 3 for i in range(400))})
    monkeypatch.setattr(net, "get", lambda *a, **k: fake_response(page("", head=f'<script type="application/ld+json">{ld}</script>')))
    text, _ = article.fetch("https://x.in/d", "")
    assert len(text) <= article.MAX_CHARS


def test_prefetch_warms_the_cache_in_parallel(monkeypatch, fake_response):
    seen = []
    monkeypatch.setattr(net, "get", lambda url, **k: seen.append(url) or fake_response("<html></html>"))
    items = [{"url": f"https://x.in/p{i}", "summary": "s"} for i in range(5)]
    article.prefetch(items)
    assert sorted(seen) == sorted(i["url"] for i in items)
    article.prefetch(items)                       # all cached now
    assert len(seen) == 5
