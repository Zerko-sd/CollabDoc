"""Operational Transformation engine (WBS 5.1, SRS 3.3 Real-time Text Sync).

A text operation is a sequence of components applied to a document of known
length:

    retain(n)   n > 0   keep the next n characters
    delete(n)   n > 0   drop the next n characters
    insert(s)           splice s in at the cursor

On the wire an operation is a flat JSON array using a compact encoding: a
positive integer is a retain, a negative integer is a delete of that many
characters, and a string is an insert -- e.g. ``[5, -2, "hi", 9]``. That array
is exactly what lands in ``document_operations.delta_json`` (SRS 6.4).

The property that makes concurrent editing safe is:

    a2, b2 = transform(a, b)  =>  apply(apply(d, a), b2) == apply(apply(d, b), a2)

so two clients that started from the same document converge on the same text no
matter which order the server happened to receive their edits in.
"""
from __future__ import annotations

import json
from typing import Any

Component = int | str


def is_retain(op: Component | None) -> bool:
    return isinstance(op, int) and op > 0


def is_delete(op: Component | None) -> bool:
    return isinstance(op, int) and op < 0


def is_insert(op: Component | None) -> bool:
    return isinstance(op, str)


class OTError(ValueError):
    """Raised when an operation cannot be applied or transformed."""


class TextOperation:
    """An ordered list of components plus the lengths they bridge."""

    __slots__ = ("ops", "base_length", "target_length")

    def __init__(self, ops: list[Component] | None = None) -> None:
        self.ops: list[Component] = []
        self.base_length = 0
        self.target_length = 0
        for op in ops or []:
            if is_retain(op):
                self.retain(op)
            elif is_delete(op):
                self.delete(-op)
            elif is_insert(op):
                self.insert(op)
            elif op == 0 or op == "":
                continue  # no-op component, drop it
            else:
                raise OTError("unsupported operation component: %r" % (op,))

    # -- builders ---------------------------------------------------------
    # Each builder merges into the trailing component where it can, which keeps
    # operations canonical so equality and the wire format stay compact.

    def retain(self, n: int) -> "TextOperation":
        if n == 0:
            return self
        if n < 0:
            raise OTError("retain expects a non-negative length")
        self.base_length += n
        self.target_length += n
        if self.ops and is_retain(self.ops[-1]):
            self.ops[-1] += n
        else:
            self.ops.append(n)
        return self

    def insert(self, text: str) -> "TextOperation":
        if text == "":
            return self
        self.target_length += len(text)
        ops = self.ops
        if ops and is_insert(ops[-1]):
            ops[-1] += text
        elif ops and is_delete(ops[-1]):
            # Keep inserts ahead of deletes so two operations with the same
            # effect also have the same representation.
            if len(ops) > 1 and is_insert(ops[-2]):
                ops[-2] += text
            else:
                ops.insert(len(ops) - 1, text)
        else:
            ops.append(text)
        return self

    def delete(self, n: int) -> "TextOperation":
        if n == 0:
            return self
        if n < 0:
            raise OTError("delete expects a non-negative length")
        self.base_length += n
        if self.ops and is_delete(self.ops[-1]):
            self.ops[-1] -= n
        else:
            self.ops.append(-n)
        return self

    # -- core semantics ----------------------------------------------------

    def apply(self, doc: str) -> str:
        if len(doc) != self.base_length:
            raise OTError(
                "operation expects a document of length %d, got %d"
                % (self.base_length, len(doc))
            )
        parts: list[str] = []
        cursor = 0
        for op in self.ops:
            if is_retain(op):
                parts.append(doc[cursor : cursor + op])
                cursor += op
            elif is_insert(op):
                parts.append(op)
            else:
                cursor -= op
        return "".join(parts)

    def invert(self, doc: str) -> "TextOperation":
        """The operation undoing this one, applied to ``self.apply(doc)``.

        This is what backs per-user undo (SRS 3.3 Undo/Redo).
        """
        inverse = TextOperation()
        cursor = 0
        for op in self.ops:
            if is_retain(op):
                inverse.retain(op)
                cursor += op
            elif is_insert(op):
                inverse.delete(len(op))
            else:
                inverse.insert(doc[cursor : cursor - op])
                cursor -= op
        return inverse

    def is_noop(self) -> bool:
        return not self.ops or (len(self.ops) == 1 and is_retain(self.ops[0]))

    # -- composition and transformation ------------------------------------

    @staticmethod
    def compose(a: "TextOperation", b: "TextOperation") -> "TextOperation":
        """One operation with the effect of applying ``a`` and then ``b``."""
        if a.target_length != b.base_length:
            raise OTError(
                "cannot compose: the first operation's output length does not "
                "match the second operation's input length"
            )
        out = TextOperation()
        it1, it2 = _Cursor(a.ops), _Cursor(b.ops)
        op1, op2 = it1.next(), it2.next()
        while True:
            if op1 is None and op2 is None:
                break
            if is_delete(op1):
                out.delete(-op1)
                op1 = it1.next()
                continue
            if is_insert(op2):
                out.insert(op2)
                op2 = it2.next()
                continue
            if op1 is None:
                raise OTError("cannot compose: first operation is too short")
            if op2 is None:
                raise OTError("cannot compose: first operation is too long")

            if is_retain(op1) and is_retain(op2):
                if op1 > op2:
                    out.retain(op2)
                    op1 -= op2
                    op2 = it2.next()
                elif op1 == op2:
                    out.retain(op1)
                    op1, op2 = it1.next(), it2.next()
                else:
                    out.retain(op1)
                    op2 -= op1
                    op1 = it1.next()
            elif is_insert(op1) and is_delete(op2):
                if len(op1) > -op2:
                    op1 = op1[-op2:]
                    op2 = it2.next()
                elif len(op1) == -op2:
                    op1, op2 = it1.next(), it2.next()
                else:
                    op2 += len(op1)
                    op1 = it1.next()
            elif is_insert(op1) and is_retain(op2):
                if len(op1) > op2:
                    out.insert(op1[:op2])
                    op1 = op1[op2:]
                    op2 = it2.next()
                elif len(op1) == op2:
                    out.insert(op1)
                    op1, op2 = it1.next(), it2.next()
                else:
                    out.insert(op1)
                    op2 -= len(op1)
                    op1 = it1.next()
            elif is_retain(op1) and is_delete(op2):
                if op1 > -op2:
                    out.delete(-op2)
                    op1 += op2
                    op2 = it2.next()
                elif op1 == -op2:
                    out.delete(op1)
                    op1, op2 = it1.next(), it2.next()
                else:
                    out.delete(op1)
                    op2 += op1
                    op1 = it1.next()
            else:  # pragma: no cover - the cases above are exhaustive
                raise OTError("cannot compose components %r and %r" % (op1, op2))
        return out

    @staticmethod
    def transform(
        a: "TextOperation", b: "TextOperation"
    ) -> tuple["TextOperation", "TextOperation"]:
        """Transform two concurrent operations against each other.

        Both operations must have been written against the same document
        state. Returns ``(a_prime, b_prime)`` such that ``a then b_prime`` and
        ``b then a_prime`` produce the same document. Concurrent inserts at the
        same position are ordered deterministically -- ``a`` goes first -- so
        every replica breaks the tie the same way.
        """
        if a.base_length != b.base_length:
            raise OTError(
                "cannot transform operations written against different "
                "document states"
            )
        a_prime, b_prime = TextOperation(), TextOperation()
        it1, it2 = _Cursor(a.ops), _Cursor(b.ops)
        op1, op2 = it1.next(), it2.next()
        while True:
            if op1 is None and op2 is None:
                break
            if is_insert(op1):
                a_prime.insert(op1)
                b_prime.retain(len(op1))
                op1 = it1.next()
                continue
            if is_insert(op2):
                a_prime.retain(len(op2))
                b_prime.insert(op2)
                op2 = it2.next()
                continue
            if op1 is None:
                raise OTError("cannot transform: first operation is too short")
            if op2 is None:
                raise OTError("cannot transform: first operation is too long")

            if is_retain(op1) and is_retain(op2):
                # Both sides keep the text: the shorter run sets the stride.
                if op1 > op2:
                    shared = op2
                    op1 -= op2
                    op2 = it2.next()
                elif op1 == op2:
                    shared = op1
                    op1, op2 = it1.next(), it2.next()
                else:
                    shared = op1
                    op2 -= op1
                    op1 = it1.next()
                a_prime.retain(shared)
                b_prime.retain(shared)
            elif is_delete(op1) and is_delete(op2):
                # Both sides removed the same characters, so neither prime
                # repeats the deletion.
                if -op1 > -op2:
                    op1 -= op2
                    op2 = it2.next()
                elif op1 == op2:
                    op1, op2 = it1.next(), it2.next()
                else:
                    op2 -= op1
                    op1 = it1.next()
            elif is_delete(op1) and is_retain(op2):
                if -op1 > op2:
                    shared = op2
                    op1 += op2
                    op2 = it2.next()
                elif -op1 == op2:
                    shared = op2
                    op1, op2 = it1.next(), it2.next()
                else:
                    shared = -op1
                    op2 += op1
                    op1 = it1.next()
                a_prime.delete(shared)
            else:  # retain against delete
                if op1 > -op2:
                    shared = -op2
                    op1 += op2
                    op2 = it2.next()
                elif op1 == -op2:
                    shared = op1
                    op1, op2 = it1.next(), it2.next()
                else:
                    shared = op1
                    op2 += op1
                    op1 = it1.next()
                b_prime.delete(shared)
        return a_prime, b_prime

    def transform_cursor(self, position: int) -> int:
        """Where a caret at ``position`` ends up once this operation lands.

        Keeps remote presence carets (SRS 3.3) pinned to the text they were
        sitting next to rather than to a stale character offset.
        """
        index = position
        cursor = 0
        for op in self.ops:
            if cursor > position:
                break
            if is_retain(op):
                cursor += op
            elif is_insert(op):
                index += len(op)
            else:
                removed = -op
                index -= min(removed, max(0, position - cursor))
                cursor += removed
        return max(0, index)

    # -- serialisation -----------------------------------------------------

    def to_json(self) -> list[Component]:
        return list(self.ops)

    def to_json_str(self) -> str:
        return json.dumps(self.ops, separators=(",", ":"))

    @classmethod
    def from_json(cls, data: Any) -> "TextOperation":
        """Parse an operation from a JSON array or its string form.

        Every malformed input leaves here as an :class:`OTError`, so callers
        handling untrusted client frames have one exception to catch.
        """
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError as exc:
                raise OTError("operation was not valid JSON: %s" % exc) from exc
        if not isinstance(data, list):
            raise OTError("an operation must be a JSON array")
        return cls(data)

    @classmethod
    def from_diff(cls, old: str, new: str) -> "TextOperation":
        """The minimal single-splice operation between two strings.

        Editors that report whole-buffer values rather than keystrokes use this
        to recover the delta.
        """
        prefix = 0
        limit = min(len(old), len(new))
        while prefix < limit and old[prefix] == new[prefix]:
            prefix += 1
        suffix = 0
        while (
            suffix < limit - prefix
            and old[len(old) - 1 - suffix] == new[len(new) - 1 - suffix]
        ):
            suffix += 1
        op = cls()
        op.retain(prefix)
        op.delete(len(old) - prefix - suffix)
        op.insert(new[prefix : len(new) - suffix])
        op.retain(suffix)
        return op

    def __eq__(self, other: object) -> bool:
        return isinstance(other, TextOperation) and self.ops == other.ops

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "TextOperation(%r)" % (self.ops,)


class _Cursor:
    """Walks a component list, yielding ``None`` once past the end."""

    __slots__ = ("_ops", "_i")

    def __init__(self, ops: list[Component]) -> None:
        self._ops = ops
        self._i = 0

    def next(self) -> Component | None:
        if self._i >= len(self._ops):
            return None
        op = self._ops[self._i]
        self._i += 1
        return op
