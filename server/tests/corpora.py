"""Synthetic writing samples with deliberately different styles.

The attribution tests need authors whose prose actually differs in the ways
stylometry measures -- sentence length, function-word habit, punctuation,
contractions -- rather than in topic, which the feature set ignores by design.
Each generator below is a caricature of one writing style, drawn from a seeded
RNG so a failing accuracy assertion is reproducible.
"""
from __future__ import annotations

import random

# -- the academic: long sentences, subordinate clauses, no contractions ----
_ACADEMIC_OPENERS = (
    "It should be noted that",
    "Furthermore, it is evident that",
    "In the context of this analysis,",
    "Moreover, the literature suggests that",
    "Consequently, one observes that",
)
_ACADEMIC_BODIES = (
    "the underlying methodology, which has been extensively documented, remains "
    "fundamentally sound",
    "the observed correlation between these variables, however marginal, warrants "
    "considerable further investigation",
    "such considerations, taken together with the preceding argument, substantiate "
    "the original hypothesis",
    "the theoretical framework, notwithstanding its limitations, accommodates these "
    "findings adequately",
)

# -- the blogger: short sentences, contractions, exclamation marks ---------
_BLOG_LINES = (
    "Here's the thing. It's simple.",
    "I don't buy it. Not one bit!",
    "So we shipped it. It works!",
    "Look, it's fine. Really.",
    "We tried. We failed. We tried again!",
    "That's it. That's the whole trick.",
)

# -- the analyst: questions, parentheses, figures -------------------------
_ANALYST_LINES = (
    "What does the data show (so far)? Growth of 12 percent.",
    "Is that sustainable (given the base)? Probably not.",
    "Where did the 40 percent come from (see note 3)?",
    "Why now (and not in Q2)? The timing is unclear.",
    "How many users churned (net of returns)? About 800.",
)


def academic(rng: random.Random, sentences: int = 3) -> str:
    return " ".join(
        "%s %s." % (rng.choice(_ACADEMIC_OPENERS), rng.choice(_ACADEMIC_BODIES))
        for _ in range(sentences)
    )


def blogger(rng: random.Random, sentences: int = 4) -> str:
    return " ".join(rng.choice(_BLOG_LINES) for _ in range(sentences))


def analyst(rng: random.Random, sentences: int = 3) -> str:
    return " ".join(rng.choice(_ANALYST_LINES) for _ in range(sentences))


STYLES = {"academic": academic, "blogger": blogger, "analyst": analyst}


def build(styles: list[str], samples: int, seed: int = 4242) -> dict[str, list[str]]:
    """``{style_name: [sample, ...]}`` for the named styles."""
    rng = random.Random(seed)
    return {
        name: [STYLES[name](rng) for _ in range(samples)] for name in styles
    }
