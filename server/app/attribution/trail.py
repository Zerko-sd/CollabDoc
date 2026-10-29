"""Per-character authorship ledger -- the "keystroke data" of SRS 3.2.

The ledger records which user typed each character currently in the document.
It is maintained by replaying the same operations the OT engine applies, so it
stays exactly in step with the text.

Two things depend on it:

* it is the *training signal* for the stylometric model -- text the ledger
  proves a user typed becomes that user's labelled corpus;
* it is a second, independent line of evidence at inference time, which the
  engine blends with the model's stylistic opinion (SRS 3.2: "analyse writing
  style & keystroke data").

Storage is run-length encoded, so a paragraph typed by one person costs one
entry rather than one entry per character.
"""
from __future__ import annotations

from .. realtime.ot import TextOperation, is_delete, is_insert, is_retain

Run = list  # [length, user_id]


class AuthorTrail:
    """Run-length encoded map from character offset to the user who typed it."""

    __slots__ = ("runs",)

    def __init__(self, runs: list[Run] | None = None) -> None:
        self.runs: list[Run] = [list(r) for r in (runs or [])]

    @property
    def length(self) -> int:
        return sum(run[0] for run in self.runs)

    # -- mutation ----------------------------------------------------------

    def apply(self, operation: TextOperation, user_id: str) -> None:
        """Advance the ledger by one accepted operation typed by ``user_id``."""
        result: list[Run] = []
        reader = _RunReader(self.runs)
        for op in operation.ops:
            if is_retain(op):
                for run in reader.take(op):
                    _append(result, run[0], run[1])
            elif is_insert(op):
                _append(result, len(op), user_id)
            elif is_delete(op):
                reader.take(-op)
        # Anything the operation never mentioned keeps its existing owner.
        for run in reader.rest():
            _append(result, run[0], run[1])
        self.runs = result

    def reset(self, length: int, user_id: str | None) -> None:
        self.runs = [[length, user_id]] if length > 0 else []

    # -- queries -----------------------------------------------------------

    def owner_at(self, index: int) -> str | None:
        cursor = 0
        for size, user in self.runs:
            if index < cursor + size:
                return user
            cursor += size
        return None

    def share_of_range(self, start: int, end: int) -> dict[str, float]:
        """Fraction of characters in ``[start, end)`` owned by each user."""
        span = max(0, end - start)
        if span == 0:
            return {}
        tally: dict[str, int] = {}
        cursor = 0
        for size, user in self.runs:
            run_start, run_end = cursor, cursor + size
            cursor = run_end
            if run_end <= start:
                continue
            if run_start >= end:
                break
            overlap = min(run_end, end) - max(run_start, start)
            if overlap > 0 and user is not None:
                tally[user] = tally.get(user, 0) + overlap
        return {user: count / span for user, count in tally.items()}

    def dominant_of_range(self, start: int, end: int) -> tuple[str | None, float]:
        """The user who typed most of a range, and their share of it."""
        share = self.share_of_range(start, end)
        if not share:
            return None, 0.0
        user = max(share, key=share.__getitem__)
        return user, share[user]

    def text_by_author(self, document: str) -> dict[str, list[str]]:
        """Split the document into the contiguous stretches each user typed."""
        chunks: dict[str, list[str]] = {}
        cursor = 0
        for size, user in self.runs:
            if user is not None:
                chunks.setdefault(user, []).append(document[cursor : cursor + size])
            cursor += size
        return chunks

    # -- serialisation -----------------------------------------------------

    def to_json(self) -> list[Run]:
        return [list(run) for run in self.runs]

    @classmethod
    def from_json(cls, data: list[Run] | None) -> "AuthorTrail":
        return cls(data or [])


def _append(runs: list[Run], size: int, user: str | None) -> None:
    if size <= 0:
        return
    if runs and runs[-1][1] == user:
        runs[-1][0] += size
    else:
        runs.append([size, user])


class _RunReader:
    """Consumes runs a character-count at a time, splitting where needed."""

    __slots__ = ("_runs", "_i", "_offset")

    def __init__(self, runs: list[Run]) -> None:
        self._runs = runs
        self._i = 0
        self._offset = 0

    def take(self, count: int) -> list[Run]:
        taken: list[Run] = []
        remaining = count
        while remaining > 0 and self._i < len(self._runs):
            size, user = self._runs[self._i]
            available = size - self._offset
            step = min(available, remaining)
            taken.append([step, user])
            remaining -= step
            self._offset += step
            if self._offset >= size:
                self._i += 1
                self._offset = 0
        return taken

    def rest(self) -> list[Run]:
        remainder: list[Run] = []
        if self._i < len(self._runs):
            size, user = self._runs[self._i]
            if size - self._offset > 0:
                remainder.append([size - self._offset, user])
            remainder.extend(list(r) for r in self._runs[self._i + 1 :])
        return remainder
