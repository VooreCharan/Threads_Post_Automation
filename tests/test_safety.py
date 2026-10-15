import time

import pytest

from app import config, safety, state

MUST_CATCH = [
    "Man murdered in Delhi over parking row", "Teen dies after fall from building", "Woman gangraped in moving car",
    "Terror attack kills 12 in Kashmir", "Student dies by suicide at coaching hub", "Bomb blast near market",
    "Veteran actor passes away at 80", "Death toll rises to 40 in floods", "Mob lynching in village",
    "POCSO case filed against teacher", "Stampede at temple leaves 5 dead", "Gunmen attack police post",
    "Actress's funeral held in Mumbai", "Goldsmith ends life outside women’s police station",
    "Youth took his own life", "Woman found hanging at home", "Cylinder blast kills two",
]
MUST_PASS = [
    "Bombay High Court dismisses plea on stage show", "Film shooting begins in Hyderabad for Prabhas starrer",
    "Patriot Act tweaks debated in Lok Sabha", "RBI holds repo rate; Sensex jumps 400 points",
    "Drishyam 3 box office: Ajay Devgn's thriller crosses Rs 330 crore", "Kohli hits century as India beat Australia",
    "Cardiac arrest: what to do if someone collapses", "Rain disrupts match in Perth",
    "Monsoon sets in over Kerala; yellow alert issued", "RBI ends rate-hike cycle", "Kohli ends drought with a century",
    "Life insurance rules eased", "Startup funding hits record in Q3",
]


@pytest.mark.parametrize("headline", MUST_CATCH)
def test_sensitive_headlines_are_caught(headline):
    assert safety.sensitive_match(headline)


@pytest.mark.parametrize("headline", MUST_PASS)
def test_ordinary_news_is_not_caught(headline):
    assert safety.sensitive_match(headline) is None


def test_warmup_limits_follow_account_age():
    config.ACCOUNT_WARMUP = True
    state.put("account_since", str(time.time()))
    assert safety.daily_limit_now() == 3                       # first 3 days
    state.put("account_since", str(time.time() - 4 * 86400))
    assert safety.daily_limit_now() == 5                       # until day 7
    state.put("account_since", str(time.time() - 8 * 86400))
    assert safety.daily_limit_now() == config.DAILY_POST_LIMIT


def test_a_new_account_id_restarts_the_warmup(monkeypatch):
    safety.init_account()
    state.put("account_since", str(time.time() - 30 * 86400))
    safety.init_account()                                      # same account: nothing changes
    assert safety.account_age_days() > 29
    monkeypatch.setattr(config, "THREADS_USER_ID", "acct-2")
    safety.init_account()
    assert safety.account_age_days() < 0.01


def test_daily_cap_blocks_with_a_reason():
    for i in range(config.DAILY_POST_LIMIT):
        state.record_post(f"https://x/{i}", "p", "india", f"t{i}")
    reason = safety.blocked_reason(check_pause=False)
    assert reason and "Daily limit" in reason


def test_minimum_pause_between_posts():
    state.record_post("https://x/1", "p", "india", "t")
    assert "wait" in safety.blocked_reason()
    assert safety.blocked_reason(check_pause=False) is None    # the pause is only checked at approval
    config.MIN_SECONDS_BETWEEN_POSTS = 0
    assert safety.blocked_reason() is None
