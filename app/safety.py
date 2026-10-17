"""Rules that keep an automated news account safe: no sensitive stories and posting limits.

These are deliberately conservative. They exist to keep the account within Threads' Community
Standards (no graphic or harmful content, no spam-like posting) and are all configurable in config.py.
"""
import re
import time

from . import config, state

# Stories about violence, sexual crime, suicide, deaths and other tragedies. Matched on the headline
# and feed summary before any AI is used; the AI writer also flags such stories on the full text.
SENSITIVE = re.compile(r"\b(?:" + "|".join([
    r"murder(?:s|ed|er|ers|ing)?", r"kill(?:s|ed|er|ers|ing|ings)?", r"slain", r"stabb(?:ed|ing|ings)?",
    r"shot dead", r"gunned down", r"gun ?man", r"gunmen",
    r"rape[sd]?", r"gang-?rape[sd]?", r"raping", r"rapist", r"molest(?:ed|ing|ation|er)?",
    r"sexual (?:assault|abuse|harassment|violence|offence)s?", r"child abuse", r"pocso", r"trafficking",
    r"suicid(?:e|es|al)", r"self-immolat(?:ion|ed)", r"lynch(?:ed|ing)?", r"honou?r killings?",
    r"terror(?:ism|ist|ists|ism)?", r"bomb(?:s|ed|ing|ings|er|ers)?", r"massacre", r"behead(?:ed|ing)?",
    r"acid attack", r"hostages?", r"kidnap(?:ped|ping|pers?)?", r"abduct(?:ed|ion)?",
    r"riot(?:s|ers)?", r"communal (?:violence|clash(?:es)?|tension)", r"mob violence",
    r"death toll", r"dead bod(?:y|ies)", r"corpses?", r"dies", r"died", r"dead", r"deaths?",
    r"fatal(?:ly|ities)?", r"drown(?:s|ed|ing)", r"stampede", r"passes away", r"passed away", r"demise",
    r"obituary", r"last rites", r"funeral",
    # softer phrasings that publishers use for the same things
    r"ends? (?:(?:his|her|their|own) )?li(?:fe|ves)", r"ended (?:(?:his|her|their|own) )?li(?:fe|ves)",
    r"takes? (?:his|her|their|own) li(?:fe|ves)", r"took (?:his|her|their|own) li(?:fe|ves)", r"own life",
    r"found hanging", r"hanged", r"found dead", r"beaten to death", r"hacked to death", r"burnt alive",
    r"set (?:ablaze|on fire)", r"bodies found", r"body found", r"assault(?:ed)?", r"gunfire", r"opened fire",
    r"(?:bomb|gas|cylinder|firecracker|factory|boiler) (?:blast|explosion)s?", r"injured in",
    r"domestic violence", r"dowry", r"harass(?:ed|ment)", r"extortion", r"encounter killing",
]) + r")\b", re.IGNORECASE)


def sensitive_match(*texts: str) -> str | None:
    """The first sensitive word found in the given texts, or None."""
    m = SENSITIVE.search(" ".join(t for t in texts if t))
    return m.group(0).lower() if m else None


# ---- the account and its posting limits --------------------------------------------------------

def blocked_reason(check_pause: bool = True) -> str | None:
    """Why a post must wait (daily cap or minimum pause), or None if posting is fine."""
    limit = config.DAILY_POST_LIMIT
    n, oldest = state.posts_in_window(24 * 3600)
    if n >= limit and oldest is not None:
        resume = time.strftime("%H:%M", time.localtime(oldest + 24 * 3600 + 60))
        return f"Daily limit reached ({n}/{limit} posts in the last 24 hours). Try again after about {resume}."
    if check_pause:
        last = state.last_post_time()
        if last is not None:
            wait = config.MIN_SECONDS_BETWEEN_POSTS - (time.time() - last)
            if wait > 0:
                minutes = int(wait // 60) + 1
                return f"Please wait about {minutes} more minute{'s' if minutes != 1 else ''} before the next post."
    return None
