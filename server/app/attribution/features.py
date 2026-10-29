"""Stylometric feature extraction (WBS 6.1).

Turns a stretch of prose into a fixed-length vector of scale-free style
measurements. Everything here is a *rate* or a *ratio* rather than a count, so
a two-line comment and a three-paragraph section land in the same feature
space and the classifier learns writing style instead of block length.

Pure standard library on purpose: the attribution service sits on the
sub-200ms real-time path (SRS 5.1), so it must not pay a heavyweight numeric
import on every request.
"""
from __future__ import annotations

import math
import re
from collections import Counter

_WORD_RE = re.compile(r"[A-Za-z']+")
_SENTENCE_RE = re.compile(r"[.!?]+(?:\s|$)")
_VOWEL_GROUP_RE = re.compile(r"[aeiouy]+")

# Function words carry authorial habit rather than topic, which is what makes
# them the workhorse of stylometry.
FUNCTION_WORDS = (
    "the", "of", "and", "to", "a", "in", "that", "it", "is", "was",
    "for", "with", "as", "on", "but", "this", "we", "which", "so", "however",
)

PUNCTUATION = (",", ".", ";", ":", "!", "?", "-", "'", '"', "(")

FEATURE_NAMES: tuple[str, ...] = (
    "mean_word_length",
    "stdev_word_length",
    "mean_sentence_words",
    "stdev_sentence_words",
    "type_token_ratio",
    "hapax_ratio",
    "long_word_ratio",
    "short_word_ratio",
    "mean_syllables",
    "contraction_rate",
    "uppercase_ratio",
    "digit_ratio",
    "whitespace_ratio",
    "capitalised_word_ratio",
    *tuple("punct_%s" % p for p in PUNCTUATION),
    *tuple("fw_%s" % w for w in FUNCTION_WORDS),
)

FEATURE_COUNT = len(FEATURE_NAMES)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mu = _mean(values)
    return math.sqrt(sum((v - mu) ** 2 for v in values) / (len(values) - 1))


def _syllables(word: str) -> int:
    """Vowel-group approximation -- cheap, and stable enough to compare styles."""
    groups = len(_VOWEL_GROUP_RE.findall(word.lower()))
    if word.lower().endswith("e") and groups > 1:
        groups -= 1
    return max(1, groups)


def sentence_lengths(text: str) -> list[int]:
    """Word count of each sentence, splitting on terminal punctuation."""
    lengths = []
    for chunk in _SENTENCE_RE.split(text):
        count = len(_WORD_RE.findall(chunk))
        if count:
            lengths.append(count)
    return lengths


def extract(text: str) -> list[float]:
    """The stylometric vector for ``text``.

    Returns a zero vector for empty input so callers never have to special-case
    a blank block.
    """
    if not text or not text.strip():
        return [0.0] * FEATURE_COUNT

    chars = len(text)
    words = _WORD_RE.findall(text)
    lowered = [w.lower() for w in words]
    word_count = len(words) or 1
    lengths = [float(len(w)) for w in words]
    sent_lengths = [float(n) for n in sentence_lengths(text)]
    counts = Counter(lowered)
    hapax = sum(1 for w, c in counts.items() if c == 1)

    vector: list[float] = [
        _mean(lengths),
        _stdev(lengths),
        _mean(sent_lengths),
        _stdev(sent_lengths),
        len(counts) / word_count,
        hapax / word_count,
        sum(1 for w in words if len(w) > 6) / word_count,
        sum(1 for w in words if len(w) <= 3) / word_count,
        _mean([float(_syllables(w)) for w in words]),
        sum(1 for w in words if "'" in w) / word_count,
        sum(1 for c in text if c.isupper()) / chars,
        sum(1 for c in text if c.isdigit()) / chars,
        sum(1 for c in text if c.isspace()) / chars,
        sum(1 for w in words if w[:1].isupper()) / word_count,
    ]
    # Punctuation habits, per 100 characters.
    vector.extend(text.count(p) * 100.0 / chars for p in PUNCTUATION)
    # Function-word habits, per 100 words.
    vector.extend(counts.get(w, 0) * 100.0 / word_count for w in FUNCTION_WORDS)
    return vector


def describe(vector: list[float]) -> dict[str, float]:
    """Name the components of a vector, for the metrics endpoint and debugging."""
    return {name: round(value, 4) for name, value in zip(FEATURE_NAMES, vector)}
