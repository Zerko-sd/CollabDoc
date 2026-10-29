"""The writing assistant: summarise a page, improve a passage, continue a draft.

Claude does the writing when the server has Anthropic credentials. Without
them, summarising still works through a small extractive summariser -- it
picks the sentences that carry the page's most frequent ideas -- and the
actions that need a language model say so instead of pretending.

Every result is labelled with where it came from, so nobody mistakes the
local summariser for the model.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass

from .config import get_settings

logger = logging.getLogger(__name__)

ACTIONS = ("summarize", "improve", "continue", "outline")

SYSTEM = (
    "You are the writing assistant inside a collaborative document editor. "
    "Documents use lightweight Markdown: '# ' headings, '- ' bullets, '1. ' numbered items, "
    "'- [ ] ' to-dos, '> ' quotes, **bold**, *italic*, `code`. "
    "Reply with only the text to place in the document, in that same Markdown -- "
    "no preamble, no explanation, no surrounding quotes or code fences. "
    "Keep the author's voice, language and terminology."
)

INSTRUCTIONS = {
    "summarize": "Summarise the document below in 3 to 5 bullet points that capture its key points and decisions.",
    "improve": "Rewrite the passage below for clarity, grammar and flow. Keep its meaning, structure and Markdown; do not add new facts.",
    "continue": "Continue the document below with the next one or two paragraphs, as its author would write them. Do not repeat what is already there.",
    "outline": "Propose a heading outline (## and ### headings, each with a one-line description in a bullet below it) for the document below.",
}


class AssistantUnavailable(Exception):
    """The action needs a language model and none is configured."""


@dataclass
class Suggestion:
    text: str
    source: str  # "claude" or "local"
    model: str | None = None


# -- local fallback ------------------------------------------------------------

_STOP = set(
    "a an and are as at be been but by for from has have he her his i if in into is it its "
    "me my not of on or our she so that the their them then there these they this to was "
    "we were what when which who will with would you your can just also than too very".split()
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def _plain(markup: str) -> list[str]:
    """Body lines with their markup removed, skipping code and tables."""
    lines, in_code = [], False
    for line in markup.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not stripped or stripped.startswith(("|", "![", "---")):
            continue
        stripped = re.sub(r"^(#{1,3} |[-*+] \[[ xX]\] |[-*+] |\d+[.)] |> )", "", stripped)
        stripped = re.sub(r"@\[([^\]]+)\]\(user:[^)]+\)", r"@\1", stripped)
        stripped = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", stripped)
        lines.append(re.sub(r"(\*\*|__|~~|\*|`)", "", stripped))
    return lines


def local_summary(markup: str, limit: int = 5) -> str:
    """The highest-scoring sentences, in document order, as bullets.

    A sentence scores the average document frequency of its content words,
    which favours sentences about what the page keeps coming back to.
    """
    sentences = [s.strip() for line in _plain(markup) for s in _SENTENCE.split(line) if len(s.split()) >= 4]
    if not sentences:
        raise AssistantUnavailable("There is not enough prose on this page to summarise.")
    words = lambda text: [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _STOP]  # noqa: E731
    frequency = Counter(w for s in sentences for w in words(s))
    scored = [
        (sum(frequency[w] for w in words(s)) / max(1, len(words(s))), index, s)
        for index, s in enumerate(sentences)
    ]
    best = sorted(sorted(scored, reverse=True)[:limit], key=lambda item: item[1])
    return "\n".join("- %s" % sentence for _, _, sentence in best)


# -- Claude --------------------------------------------------------------------


def _client():
    """An async Anthropic client, or None when the SDK or credentials are missing."""
    try:
        import anthropic
    except ImportError:
        return None
    try:
        return anthropic.AsyncAnthropic()
    except anthropic.AnthropicError:
        return None


async def _claude(action: str, text: str, context: str) -> Suggestion | None:
    """Ask Claude; None when no client can be made (so the caller falls back)."""
    import anthropic

    client = _client()
    if client is None:
        return None
    settings = get_settings()
    prompt = INSTRUCTIONS[action]
    if context and context != text:
        prompt += "\n\nThe rest of the document, for context only:\n<context>\n%s\n</context>" % context[:60_000]
    prompt += "\n\n<text>\n%s\n</text>" % text[:60_000]
    try:
        response = await client.beta.messages.create(
            model=settings.ai_model,
            max_tokens=16000,
            system=SYSTEM,
            # Short editorial tasks: low effort keeps them quick and cheap.
            output_config={"effort": "low"},
            # If a safety classifier declines, retry on a fallback model
            # rather than failing the request.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError:
        logger.warning("Anthropic credentials were rejected; using the local assistant")
        return None
    except TypeError as exc:
        # The SDK's way of saying no key, token or profile was found.
        if "authentication" in str(exc).lower():
            return None
        raise
    except anthropic.RateLimitError as exc:
        raise AssistantUnavailable("The AI service is busy. Try again in a minute.") from exc
    except anthropic.APIStatusError as exc:
        raise AssistantUnavailable("The AI service returned an error (%d)." % exc.status_code) from exc
    except anthropic.APIConnectionError as exc:
        raise AssistantUnavailable("Could not reach the AI service.") from exc

    if response.stop_reason == "refusal":
        raise AssistantUnavailable("The AI declined to help with this text.")
    reply = "".join(block.text for block in response.content if block.type == "text").strip()
    if not reply:
        raise AssistantUnavailable("The AI returned nothing for this text.")
    return Suggestion(text=reply, source="claude", model=response.model)


async def suggest(action: str, text: str, context: str = "") -> Suggestion:
    if action not in ACTIONS:
        raise ValueError("unknown action %r" % action)
    if not text.strip():
        raise AssistantUnavailable("There is no text to work with.")
    provider = get_settings().ai_provider
    if provider in ("auto", "claude"):
        result = await _claude(action, text, context)
        if result is not None:
            return result
        if provider == "claude":
            raise AssistantUnavailable("No Anthropic credentials are configured on this server.")
    if action == "summarize":
        return Suggestion(text=local_summary(text), source="local")
    raise AssistantUnavailable(
        "This needs the AI service. Set ANTHROPIC_API_KEY on the server to enable it; "
        "summaries work without it."
    )
