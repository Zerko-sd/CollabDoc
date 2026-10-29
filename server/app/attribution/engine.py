"""AI authorship attribution service (WBS 6.3, SRS 3.3).

Given a document, the authorship ledger, and a corpus of text each
collaborator is known to have written, the engine decides who wrote each block
and how sure it is.

It weighs two independent lines of evidence, which is what SRS 3.2 asks for
when it says the engine analyses "writing pattern & keystroke data":

*style*   -- the stylometric classifier's opinion, formed only from how the
             block is written;
*ledger*  -- the share of the block's characters the keystroke ledger records
             against each user.

Blending matters because either signal alone is wrong in a common case. The
ledger cannot see that a user pasted or lightly retyped someone else's
sentence; the classifier cannot tell apart two collaborators who have not yet
written enough for their styles to separate. Style's share of the vote is
scaled by how much text the block actually offers, so a four-word heading is
decided mostly by the ledger and a full paragraph mostly by style.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..models import BlockType
from . import blocks as block_parser
from . import features
from .classifier import StylometricClassifier
from .trail import AuthorTrail

# The most weight stylometry is ever given. Held below 1.0 so a confident
# classifier can never completely overrule recorded keystrokes.
MAX_STYLE_WEIGHT = 0.65


@dataclass
class BlockAttribution:
    index: int
    block_type: BlockType
    content: str
    start: int
    end: int
    sort_order: float
    attributed_user_id: str | None
    confidence_score: float
    method: str
    style_user_id: str | None = None
    style_confidence: float = 0.0
    ledger_user_id: str | None = None
    ledger_share: float = 0.0

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "block_type": self.block_type.value,
            "content": self.content,
            "start": self.start,
            "end": self.end,
            "sort_order": self.sort_order,
            "attributed_user_id": self.attributed_user_id,
            "confidence_score": round(self.confidence_score, 4),
            "method": self.method,
            "evidence": {
                "style_user_id": self.style_user_id,
                "style_confidence": round(self.style_confidence, 4),
                "ledger_user_id": self.ledger_user_id,
                "ledger_share": round(self.ledger_share, 4),
                "agree": (
                    self.style_user_id is not None
                    and self.style_user_id == self.ledger_user_id
                ),
            },
        }


@dataclass
class TrainingReport:
    trained: bool
    authors: list[str] = field(default_factory=list)
    samples: int = 0
    accuracy: float = 0.0
    duration_ms: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "trained": self.trained,
            "authors": self.authors,
            "samples": self.samples,
            "training_accuracy": round(self.accuracy, 4),
            "duration_ms": round(self.duration_ms, 2),
            "reason": self.reason,
        }


class AttributionEngine:
    """Trains on a document's collaborators and attributes its blocks."""

    def __init__(self, min_chars: int = 120, min_samples_per_author: int = 3) -> None:
        self.min_chars = min_chars
        self.min_samples_per_author = min_samples_per_author
        self.model: StylometricClassifier | None = None
        self.report = TrainingReport(trained=False, reason="not trained yet")

    # -- training ----------------------------------------------------------

    def chunk(self, text: str) -> list[str]:
        """Cut one author's writing into samples of at least ``min_chars``.

        Splitting on sentence boundaries rather than a fixed character count
        keeps each sample a natural piece of prose, so the sentence-length and
        punctuation features measure something real.
        """
        samples: list[str] = []
        buffer: list[str] = []
        size = 0
        for piece in _split_sentences(text):
            buffer.append(piece)
            size += len(piece)
            if size >= self.min_chars:
                samples.append("".join(buffer).strip())
                buffer, size = [], 0
        tail = "".join(buffer).strip()
        if tail:
            if len(tail) >= self.min_chars // 2 or not samples:
                samples.append(tail)
            else:
                samples[-1] = (samples[-1] + " " + tail).strip()
        return [s for s in samples if s]

    def train(self, corpus: dict[str, list[str]]) -> TrainingReport:
        """Fit the classifier on ``{user_id: [text, ...]}``."""
        started = time.perf_counter()
        vectors: list[list[float]] = []
        labels: list[str] = []
        usable_authors: list[str] = []

        for user_id, texts in corpus.items():
            samples: list[str] = []
            for text in texts:
                samples.extend(self.chunk(text))
            if len(samples) < self.min_samples_per_author:
                continue
            usable_authors.append(user_id)
            for sample in samples:
                vectors.append(features.extract(sample))
                labels.append(user_id)

        if len(usable_authors) < 2:
            self.model = None
            self.report = TrainingReport(
                trained=False,
                authors=usable_authors,
                samples=len(vectors),
                duration_ms=(time.perf_counter() - started) * 1000,
                reason=(
                    "needs at least two authors with "
                    f"{self.min_samples_per_author} samples each; "
                    "falling back to the keystroke ledger"
                ),
            )
            return self.report

        model = StylometricClassifier().fit(vectors, labels)
        self.model = model
        self.report = TrainingReport(
            trained=True,
            authors=usable_authors,
            samples=len(vectors),
            accuracy=model.score(vectors, labels),
            duration_ms=(time.perf_counter() - started) * 1000,
            reason="ok",
        )
        return self.report

    # -- inference ---------------------------------------------------------

    def attribute_text(self, text: str) -> tuple[str | None, float]:
        """The classifier's unblended opinion about a stretch of prose."""
        if self.model is None or not text.strip():
            return None, 0.0
        return self.model.predict(features.extract(text))

    def attribute_document(
        self, document: str, trail: AuthorTrail
    ) -> list[BlockAttribution]:
        """Attribute every block of ``document``."""
        results: list[BlockAttribution] = []
        for parsed in block_parser.parse(document):
            results.append(self._attribute_block(parsed, trail))
        return results

    def _attribute_block(
        self, parsed: block_parser.ParsedBlock, trail: AuthorTrail
    ) -> BlockAttribution:
        prose = parsed.text
        ledger_user, ledger_share = trail.dominant_of_range(parsed.start, parsed.end)
        style_user, style_confidence = self.attribute_text(prose)

        if style_user is None:
            # No usable model: the ledger is the only evidence there is.
            return BlockAttribution(
                index=parsed.index,
                block_type=parsed.block_type,
                content=parsed.content,
                start=parsed.start,
                end=parsed.end,
                sort_order=parsed.sort_order,
                attributed_user_id=ledger_user,
                confidence_score=ledger_share,
                method="ledger",
                ledger_user_id=ledger_user,
                ledger_share=ledger_share,
            )

        # Short blocks give stylometry little to work with, so its vote fades
        # out linearly as the block gets shorter than one training sample.
        style_weight = MAX_STYLE_WEIGHT * min(1.0, len(prose) / max(1, self.min_chars))
        ledger_weight = 1.0 - style_weight

        style_probs = self.model.predict_proba(features.extract(prose))
        ledger_probs = trail.share_of_range(parsed.start, parsed.end)
        combined: dict[str, float] = {}
        for user in set(style_probs) | set(ledger_probs):
            combined[user] = (
                style_weight * style_probs.get(user, 0.0)
                + ledger_weight * ledger_probs.get(user, 0.0)
            )

        total = sum(combined.values())
        if total <= 0:
            winner, confidence = ledger_user, ledger_share
        else:
            winner = max(combined, key=combined.__getitem__)
            confidence = combined[winner] / total

        return BlockAttribution(
            index=parsed.index,
            block_type=parsed.block_type,
            content=parsed.content,
            start=parsed.start,
            end=parsed.end,
            sort_order=parsed.sort_order,
            attributed_user_id=winner,
            confidence_score=confidence,
            method="hybrid",
            style_user_id=style_user,
            style_confidence=style_confidence,
            ledger_user_id=ledger_user,
            ledger_share=ledger_share,
        )


def _split_sentences(text: str) -> list[str]:
    """Break text after terminal punctuation, keeping the punctuation."""
    pieces: list[str] = []
    current: list[str] = []
    for char in text:
        current.append(char)
        if char in ".!?\n":
            pieces.append("".join(current))
            current = []
    if current:
        pieces.append("".join(current))
    return pieces
