"""The whole Telegram workflow (preview, approve, skip, limits) with a fake news feed, fake AI, fake
Threads and a recorder instead of Telegram."""
import time

import pytest

from app import article, bot, config, drafts, feeds, images, llm, safety, state, telegram_bot as tg, threads

BODY = "x" * 400
STORIES = [
    ("Man murdered in Delhi over parking row", BODY),                  # sensitive (keyword)
    ("Tiny story", "short"),                                           # too little readable text
    ("Kohli hits century as India beat Australia", BODY),
    ("PV Sindhu enters Japan Open quarterfinals", BODY),
    ("Neeraj Chopra wins Diamond League title", BODY),
    ("Hockey India names squad for Asian Games", BODY),
]


class World:
    def __init__(self):
        self.items = [{"url": f"https://thehindu.com/sports/{i}", "title": t, "summary": "s", "source": "The Hindu",
                       "category": "sports", "published": 9e9 - i, "image": ""} for i, (t, _) in enumerate(STORIES)]
        self.text = {it["url"]: STORIES[i][1] for i, it in enumerate(self.items)}
        self.published = []      # (text, link, image_url) of every Threads publish
        self.calls = []          # every Telegram API call: (method, params)
        self._mid = 100

    def add(self, title: str) -> dict:
        item = {"url": f"https://thehindu.com/sports/{len(self.items) + 50}", "title": title, "summary": "s",
                "source": "The Hindu", "category": "sports", "published": 9e9 + len(self.items), "image": ""}
        self.items.append(item)
        self.text[item["url"]] = BODY
        return item


@pytest.fixture
def world(monkeypatch):
    w = World()
    monkeypatch.setattr(feeds, "fetch_category", lambda c: [dict(i) for i in w.items] if c == "sports" else [])
    monkeypatch.setattr(feeds, "article_text", lambda i: w.text[i["url"]])
    monkeypatch.setattr(article, "prefetch", lambda items, workers=4: None)
    monkeypatch.setattr(llm, "write_post", lambda i, c: llm.Written(f"Post about {i['title']}.", ["ViratKohli"], "", True, False, False))
    monkeypatch.setattr(images, "pick_image", lambda item, post="", q="": (
        ("https://img.example/stock.jpg", "stock", "Stock photo: Jane / Pexels") if "Sindhu" in item["title"] else (None, None, None)))
    monkeypatch.setattr(threads, "publish", lambda text, link=None, image_url=None:
                        (w.published.append((text, link, image_url)) or f"id{len(w.published)}"))
    monkeypatch.setattr(threads, "permalink", lambda pid: f"https://www.threads.com/@newaccount/post/{pid}")

    def fake_call(method, http_timeout=40, **params):
        w.calls.append((method, params))
        if method in ("sendMessage", "sendPhoto"):
            w._mid += 1
            return {"result": {"message_id": w._mid}}
        return {"result": True}
    monkeypatch.setattr(tg, "_call", fake_call)
    monkeypatch.setattr(config, "MIN_SECONDS_BETWEEN_POSTS", 0)
    return w


def wait(pred, seconds=8):
    end = time.time() + seconds
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def say(w, text, user=42):
    w.calls.clear()
    tg._handle(1, user, text)


def tap(w, action, draft_id, user=42):
    w.calls.clear()
    tg._handle_callback({"id": "cb", "from": {"id": user}, "message": {"chat": {"id": 1}, "message_id": 7},
                         "data": f"{action}:{draft_id}"})


def texts(w):
    return [p.get("text") or p.get("caption") or "" for m, p in w.calls if m in ("sendMessage", "sendPhoto")]


def draft_messages(w):
    return [(p.get("text") or p.get("caption"), p["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1])
            for m, p in w.calls if m in ("sendMessage", "sendPhoto") and p.get("reply_markup")]


def ask_for_draft(w, category="sports"):
    say(w, f"post {category}")
    assert wait(lambda: draft_messages(w)), texts(w)
    return draft_messages(w)[0]


def answers(w):
    return [p for m, p in w.calls if m == "answerCallbackQuery"]


# ---- the greeting and the draft --------------------------------------------------------------------

def test_hi_lists_the_commands_and_explains_approval(world):
    say(world, "Hi")
    assert "post sports" in texts(world)[0] and "Nothing is posted" in texts(world)[0]


def test_post_prepares_a_draft_and_publishes_nothing(world):
    text, _ = ask_for_draft(world)
    assert "Kohli" in text and "#Sports" in text and "Source: The Hindu" in text
    assert world.published == []
    assert state.was_skipped(world.items[0]["url"]) and state.was_skipped(world.items[1]["url"])  # sensitive + thin


def test_a_second_command_proposes_a_different_story(world):
    ask_for_draft(world)
    world.calls.clear()
    text2, _ = ask_for_draft(world)
    assert "Sindhu" in text2 and any(m == "sendPhoto" for m, _ in world.calls)
    assert "Stock photo: Jane / Pexels" in text2


def test_the_number_of_waiting_drafts_is_limited(world):
    config.MAX_PENDING_DRAFTS = 1
    ask_for_draft(world)
    say(world, "post sports")
    assert "already have 1 drafts" in texts(world)[0]


# ---- approving -----------------------------------------------------------------------------------------

def test_approving_publishes_once_and_replies_with_the_threads_link(world):
    _, draft_id = ask_for_draft(world)
    tap(world, "ok", draft_id)
    assert wait(lambda: any("Posted to Threads" in t for t in texts(world)))
    assert len(world.published) == 1
    text, link, image_url = world.published[0]
    assert link == world.items[2]["url"] and image_url is None            # text post: Threads makes the link preview
    assert any("threads.com/@newaccount/post/id1" in t and world.items[2]["url"] in t for t in texts(world))
    assert any(m == "editMessageReplyMarkup" for m, _ in world.calls)      # buttons removed
    assert state.was_posted(world.items[2]["url"])


def test_a_double_tap_cannot_publish_twice(world):
    _, draft_id = ask_for_draft(world)
    tap(world, "ok", draft_id)
    assert wait(lambda: len(world.published) == 1)
    tap(world, "ok", draft_id)
    assert len(world.published) == 1 and "expired" in answers(world)[0]["text"]


def test_the_pause_between_posts_is_enforced_when_you_approve(world):
    _, first = ask_for_draft(world)
    tap(world, "ok", first)
    assert wait(lambda: len(world.published) == 1)
    config.MIN_SECONDS_BETWEEN_POSTS = 300
    _, second = ask_for_draft(world)
    tap(world, "ok", second)
    assert len(world.published) == 1 and answers(world)[0]["show_alert"]
    assert drafts.get(second) is not None                                  # kept: approve later
    config.MIN_SECONDS_BETWEEN_POSTS = 0
    tap(world, "ok", second)
    assert wait(lambda: len(world.published) == 2)
    assert world.published[1][2] == "https://img.example/stock.jpg" and "Stock photo: Jane / Pexels" in world.published[1][0]
    assert world.items[3]["url"] in world.published[1][0]                  # image posts carry the link in the text


def test_a_failed_publish_is_reported_and_the_story_can_be_retried(world, monkeypatch):
    _, draft_id = ask_for_draft(world)
    monkeypatch.setattr(threads, "publish", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("token expired")))
    tap(world, "ok", draft_id)
    assert wait(lambda: any("Couldn't publish" in t and "token expired" in t for t in texts(world)))
    assert not state.was_posted(world.items[2]["url"])


# ---- skipping, expiry, restart, strangers ------------------------------------------------------------

def test_skip_discards_and_never_proposes_the_story_again(world):
    text, draft_id = ask_for_draft(world)
    tap(world, "no", draft_id)
    assert drafts.get(draft_id) is None and state.was_skipped(world.items[2]["url"])
    text2, _ = ask_for_draft(world)
    assert world.items[2]["title"] not in text2


def test_another_story_replaces_the_draft(world):
    _, draft_id = ask_for_draft(world)
    tap(world, "re", draft_id)
    assert wait(lambda: draft_messages(world))
    assert drafts.get(draft_id) is None and "Sindhu" in draft_messages(world)[0][0]


def test_waiting_drafts_survive_a_restart(world):
    _, draft_id = ask_for_draft(world)
    state.init()
    assert drafts.get(draft_id) is not None


def test_an_expired_draft_cannot_be_approved(world):
    _, draft_id = ask_for_draft(world)
    config.DRAFT_TTL_MINUTES = 0
    assert bot.approve(draft_id)["status"] == "expired"


def test_strangers_are_ignored(world):
    say(world, "post sports", user=999)
    assert texts(world) == [] and world.published == []


def test_a_stranger_cannot_approve_or_skip_your_draft(world):
    _, draft_id = ask_for_draft(world)
    for action in ("ok", "no", "re"):
        tap(world, action, draft_id, user=999)             # someone else taps the buttons on your draft
        assert world.published == [] and drafts.get(draft_id) is not None
        assert not [c for c in world.calls if c[0] in ("sendMessage", "sendPhoto", "editMessageReplyMarkup")]
    assert not state.was_skipped(world.items[2]["url"])


def test_cancel_discards_all_waiting_drafts(world):
    ask_for_draft(world)
    say(world, "cancel")
    assert "Discarded 1 waiting draft" in texts(world)[0] and drafts.count() == 0


# ---- limits -------------------------------------------------------------------------------------------

def test_warmup_caps_a_new_account_and_says_why(world):
    config.ACCOUNT_WARMUP = True
    state.put("account_since", str(time.time()))
    assert safety.daily_limit_now() == 3
    for i in range(3):
        state.record_post(f"https://filler/{i}", "p", "sports", f"Filler {i}")
    say(world, "post sports")
    assert "warm-up" in texts(world)[0]


def test_unknown_or_missing_category_shows_the_list(world):
    say(world, "post")
    assert "Which category?" in texts(world)[0]
    say(world, "post cooking")
    assert "don't know the category" in texts(world)[0] and "post sports" in texts(world)[0]


def test_category_aliases(world):
    assert tg._parse_category("tech") == "technology" and tg._parse_category("cricket") == "sports"
    assert tg._parse_category("cooking") is None and tg._parse_category("") is None


def test_with_approval_off_a_command_posts_straight_away(world):
    config.REQUIRE_APPROVAL = False
    say(world, "post sports")
    assert wait(lambda: len(world.published) == 1)
    assert not draft_messages(world)


def test_nothing_new_gives_a_clear_message(world):
    world.items.clear()
    say(world, "post sports")
    assert wait(lambda: any("No new sports stories" in t for t in texts(world)))


def test_a_story_about_the_same_event_is_skipped(world, monkeypatch):
    def writer(item, content):
        dup = "Kohli" in item["title"] and state.was_posted(world.items[2]["url"])
        return llm.Written(f"Post about {item['title']}.", [], "", True, dup, False)
    monkeypatch.setattr(llm, "write_post", writer)
    world.items[2]["title"] = "Kohli hits century as India beat Australia"
    world.add("Kohli ton powers India past Australia in Perth")
    _, first = ask_for_draft(world)
    tap(world, "ok", first)
    assert wait(lambda: len(world.published) == 1)
    say(world, "post sports")
    assert wait(lambda: draft_messages(world))
    assert "Kohli ton powers" not in draft_messages(world)[0][0]


def test_non_india_and_ai_flagged_sensitive_stories_are_skipped(world, monkeypatch):
    flags = {"Kohli": (False, False), "Sindhu": (True, True)}   # (is_india, is_sensitive)

    def writer(item, content):
        for key, (india, sensitive) in flags.items():
            if key in item["title"]:
                return llm.Written("P.", [], "", india, False, sensitive)
        return llm.Written(f"Post about {item['title']}.", [], "", True, False, False)
    monkeypatch.setattr(llm, "write_post", writer)
    text, _ = ask_for_draft(world)
    assert "Neeraj" in text
    assert state.was_skipped(world.items[2]["url"]) and state.was_skipped(world.items[3]["url"])


def test_an_unverifiable_post_is_skipped_not_published(world, monkeypatch):
    monkeypatch.setattr(llm, "write_post", lambda i, c: llm.UNVERIFIED if "Kohli" in i["title"]
                        else llm.Written("P.", [], "", True, False, False))
    text, _ = ask_for_draft(world)
    assert "Kohli" not in text and state.was_skipped(world.items[2]["url"])
