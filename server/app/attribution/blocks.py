"""Document parsing into content nodes (SRS 6.5 Document_Blocks).

The editor's buffer is plain text carrying lightweight markup, so a block is
just a line: ``# heading``, ``- list item``, or a paragraph. Parsing returns
each block together with the character range it occupies in the buffer, which
is what lets the attribution engine line a block up against the authorship
ledger without re-searching the document.

``sort_order`` is a lexical rank rather than an integer index: inserting a
block between two others only needs a new midpoint, so concurrent inserts do
not have to renumber the blocks after them (SRS 6.5).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models import BlockType

RANK_STEP = 1024.0


@dataclass(frozen=True)
class ParsedBlock:
    index: int
    block_type: BlockType
    content: str
    start: int
    end: int
    sort_order: float
    # False for code, table rows and images: they carry no writing style, so
    # attribution leaves them to the keystroke ledger.
    prose: bool = True

    @property
    def text(self) -> str:
        """The block's prose with its markup prefix stripped.

        Style is measured on this, so a heading's ``#`` does not read as
        punctuation habit.
        """
        if not self.prose:
            return ""
        stripped = self.content.lstrip()
        if self.block_type is BlockType.HEADING:
            return stripped.lstrip("#").strip()
        if self.block_type is BlockType.LIST_ITEM:
            body = stripped
            for marker in ("- ", "* ", "+ "):
                if body.startswith(marker):
                    return body[len(marker) :].strip()
            # Ordered list: "1. text"
            head, _, tail = body.partition(". ")
            if head.rstrip(")").isdigit():
                return tail.strip()
            return body.strip()
        return stripped


def classify(line: str) -> BlockType:
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return BlockType.HEADING
    if stripped[:2] in ("- ", "* ", "+ "):
        return BlockType.LIST_ITEM
    head, sep, _ = stripped.partition(". ")
    if sep and head.rstrip(")").isdigit():
        return BlockType.LIST_ITEM
    return BlockType.PARAGRAPH


def is_fence(line: str) -> bool:
    """A ``` line, opening (optionally naming a language) or closing a code block."""
    stripped = line.strip()
    return stripped.startswith("```") and "`" not in stripped[3:]


def is_structural(line: str) -> bool:
    """A table row, an image or a table of contents: content, but not prose."""
    stripped = line.strip()
    if stripped == "[TOC]":
        return True
    if len(stripped) > 1 and stripped.startswith("|") and stripped.endswith("|"):
        return True
    return stripped.startswith("![") and stripped.endswith(")")


def parse(document: str) -> list[ParsedBlock]:
    """Split a buffer into blocks, skipping blank lines."""
    blocks: list[ParsedBlock] = []
    cursor = 0
    index = 0
    in_code = False
    for line in document.split("\n"):
        start = cursor
        cursor += len(line) + 1  # the newline that split() consumed
        fence = is_fence(line)
        prose = not (in_code or fence or is_structural(line))
        if fence:
            in_code = not in_code
        if not line.strip():
            continue
        blocks.append(
            ParsedBlock(
                index=index,
                # A "# comment" inside a code block is not a heading.
                block_type=classify(line) if prose else BlockType.PARAGRAPH,
                content=line,
                start=start,
                end=start + len(line),
                sort_order=(index + 1) * RANK_STEP,
                prose=prose,
            )
        )
        index += 1
    return blocks


def rank_between(before: float | None, after: float | None) -> float:
    """A lexical rank that sorts strictly between two neighbours."""
    if before is None and after is None:
        return RANK_STEP
    if before is None:
        return after / 2.0
    if after is None:
        return before + RANK_STEP
    return (before + after) / 2.0
