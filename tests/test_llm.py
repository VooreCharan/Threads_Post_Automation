import json

import pytest

from app import alerts, config, llm

ITEM = {"url": "https://www.thehindu.com/sport/x/article1.ece", "title": "Kohli hits century as India win",
        "category": "sports", "source": "The Hindu"}
ARTICLE = ("Virat Kohli scored a century as India beat Australia by six wickets in Perth on Sunday, "
           "taking a 1-0 lead in the three-match ODI series. " * 3)


def reply(**fields) -> str:
    data = {"india": True, "duplicate": False, "sensitive": False, "post": "Kohli hit a century as India won.",
            "hashtags": ["ViratKohli", "Perth"], "photo_query": "cricket stadium"}
    data.update(fields)
    return json.dumps(data)


@pytest.fixture
def ai(monkeypatch):
    """A scripted AI: one provider; the writer and the fact-checker answer from queues."""
    monkeypatch.setitem(config.OPENAI_COMPAT["groq"], "key", "test-key")
    monkeypatch.setitem(config.OPENAI_COMPAT["groq"], "interval", 0)
    monkeypatch.setattr(config, "LLM_CHAIN", ["groq"])

    class Script:
        writes, checks, calls = [], [], []

    s = Script()
    s.writes, s.checks, s.calls = [], [], []

    def fake_call(slot, prompt):
        s.calls.append("check" if "strict fact-checker" in prompt else "write")
        queue = s.checks if "strict fact-checker" in prompt else s.writes
        if not queue:
            raise RuntimeError("503 provider unavailable")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(llm, "_call", fake_call)
    return s


CLEAN = json.dumps({"unsupported": [], "irrelevant_hashtags": []})


# ---- tags, parsing, composing ---------------------------------------------------------------------

def test_clean_tags_rules():
    out = llm.clean_tags(["#TeamIndia", "Trending", "saba azad", "Saba-Azad", "2026", "x", "Breaking", "teamindia", "Kohli!"])
    assert out == ["TeamIndia", "sabaazad", "Kohli"]
    assert llm.clean_tags(["A" * 40]) == []


def test_parse_tolerates_fences_and_string_booleans():
    raw = "Sure!\n```json\n" + reply(india="false", duplicate="TRUE", sensitive="no") + "\n```"
    w = llm._parse(raw)
    assert (w.is_india, w.is_duplicate, w.is_sensitive) == (False, True, False)
    assert w.tags == ["ViratKohli", "Perth"]


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        llm._parse("no json here")
    with pytest.raises(ValueError):
        llm._parse(reply(post="  "))


def test_compose_has_one_topic_tag_first_and_no_hash_in_body():
    text = llm.compose("Body #1 text with #hash.", ["Sports", "TeamIndia"], "NDTV Sports")
    assert text == "Body 1 text with hash.\n\n#Sports #TeamIndia\nSource: NDTV Sports"
    assert text.index("#Sports") == text.index("#")                 # the category tag is the Threads topic


def test_post_layout_is_text_then_hashtags_then_link_then_one_attribution_line():
    link = "https://www.thehindu.com/sport/a1.ece"
    # the article's own image: the credit line names the publisher, and there is no separate "Source:" line
    with_image = llm.compose("Kohli hit a century.", ["Sports", "ViratKohli"], "The Hindu", link=link,
                             photo_credit="Image: The Hindu")
    assert with_image == f"Kohli hit a century.\n\n#Sports #ViratKohli\n{link}\n\U0001F4F7 Image: The Hindu"
    assert "Source:" not in with_image
    # a stock photo (or no picture): no credit of any kind for the photo, the story's source instead
    without = llm.compose("Kohli hit a century.", ["Sports", "ViratKohli"], "The Hindu", link=link)
    assert without == f"Kohli hit a century.\n\n#Sports #ViratKohli\n{link}\nSource: The Hindu"
    assert "\U0001F4F7" not in without


def test_compose_never_exceeds_500_and_keeps_the_link():
    link = "https://www.bbc.co.uk/news/articles/" + "a" * 60
    for body in ("x" * 900, ("A full sentence here. " * 60)):
        text = llm.compose(body, ["Sports", "A", "B"], "BBC News", link=link, photo_credit="Image: BBC News")
        assert len(text) <= 500 and link in text and text.endswith("Image: BBC News")
    cut = llm.compose("First sentence is here. Second sentence is long " + "z" * 600, ["Sports"], "X")
    assert cut.startswith("First sentence is here.") and "z" not in cut   # trimmed at a sentence boundary


# ---- writing + fact-checking -----------------------------------------------------------------------

def test_a_faithful_post_is_accepted(ai):
    ai.writes.append(reply())
    ai.checks.append(CLEAN)
    w = llm.write_post(ITEM, ARTICLE)
    assert w.post.startswith("Kohli") and w.tags == ["ViratKohli", "Perth"]
    assert ai.calls == ["write", "check"]


def test_unsupported_claim_triggers_one_rewrite(ai):
    ai.writes += [reply(post="Former Pakistani cricketer Kohli won."), reply()]
    ai.checks += [json.dumps({"unsupported": ["former Pakistani cricketer"], "irrelevant_hashtags": []}), CLEAN]
    w = llm.write_post(ITEM, ARTICLE)
    assert w.post == "Kohli hit a century as India won."
    assert ai.calls == ["write", "check", "write", "check"]


def test_a_post_that_keeps_failing_the_check_is_unverified(ai):
    bad = json.dumps({"unsupported": ["invented fact"], "irrelevant_hashtags": []})
    ai.writes += [reply(), reply()]
    ai.checks += [bad, bad]
    assert llm.write_post(ITEM, ARTICLE) is llm.UNVERIFIED


def test_no_fact_check_means_nothing_is_published(ai):
    ai.writes.append(reply())            # the writer works but the checker has no answers (provider down)
    assert llm.write_post(ITEM, ARTICLE) is None


def test_irrelevant_hashtags_are_dropped(ai):
    ai.writes.append(reply(hashtags=["ViratKohli", "Perth", "Bitcoin"]))
    ai.checks.append(json.dumps({"unsupported": [], "irrelevant_hashtags": ["#Bitcoin"]}))
    assert llm.write_post(ITEM, ARTICLE).tags == ["ViratKohli", "Perth"]


@pytest.mark.parametrize("flag", ["sensitive", "duplicate"])
def test_stories_that_will_not_be_posted_skip_the_fact_check(ai, flag):
    ai.writes.append(reply(**{flag: True}))
    w = llm.write_post(ITEM, ARTICLE)
    assert (w.is_sensitive if flag == "sensitive" else w.is_duplicate) is True
    assert ai.calls == ["write"]


def test_results_are_cached_so_the_same_story_costs_no_second_call(ai):
    ai.writes.append(reply())
    ai.checks.append(CLEAN)
    first = llm.write_post(ITEM, ARTICLE)
    assert llm.write_post(ITEM, ARTICLE) == first
    assert ai.calls == ["write", "check"]          # the second call used the cache


def test_a_failed_attempt_is_not_cached(ai):
    assert llm.write_post(ITEM, ARTICLE) is None
    llm._cooldown_until.clear()                        # the provider's short cooldown is a separate feature
    ai.writes.append(reply())
    ai.checks.append(CLEAN)
    assert llm.write_post(ITEM, ARTICLE) is not None   # the earlier failure was not remembered


def test_three_failures_in_a_row_alert_the_owner(ai, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "alert", lambda kind, text, **kw: sent.append(kind))
    for _ in range(3):
        assert llm.write_post(dict(ITEM, url=ITEM["url"] + str(_)), ARTICLE) is None
        llm._cooldown_until.clear()
    assert "llm_down" in sent


# ---- trimming never leaves a dangling abbreviation ----------------------------------------------------

def test_trimming_does_not_stop_after_an_abbreviation():
    first = ("Soha Ali Khan revealed on a podcast that her mother Sharmila Tagore insisted on surgery for stage zero "
             "lung cancer after doctors found cells that turned out to be malignant, and that the family agreed with "
             "the advice of the specialists who treated her at the hospital in Mumbai last month.")
    body = first + (" Dr. Rao of AIIMS said such early surgery can cure the disease in most patients and that regular "
                    "screening matters for everyone above fifty years of age, adding many more details about it here.")
    link = "https://indianexpress.com/article/lifestyle/health/sharmila-tagore-stage-zero-lung-cancer-surgery-10917710"
    text = llm.compose(body, ["Health", "SohaAliKhan", "SharmilaTagore"], "Indian Express", link=link)
    first_line = text.split("\n\n")[0]
    assert len(body) > 500 - 170 and len(text) <= 500          # trimming really happened
    assert first_line == first, first_line                      # cut at the real sentence end, not after "Dr."


@pytest.mark.parametrize("text,expected_end", [
    ("He paid Rs. 5 crore. Next sentence here", 19),
    ("Dr. Rao spoke. Then B. Rao replied and more", 13),
    ("Costs rose vs. last year and then fell", -1),
    ("Is it so? Yes it is. And more", 19),
])
def test_last_sentence_end(text, expected_end):
    assert llm._last_sentence_end(text) == expected_end


def test_the_writer_is_asked_for_short_posts():
    assert "at most 280 characters" in llm.PROMPT
