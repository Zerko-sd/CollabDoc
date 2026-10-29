"""Operational transformation unit tests (WBS 9.1, covers WBS 5.1)."""
from __future__ import annotations

import random

import pytest

from app.realtime.ot import OTError, TextOperation


def test_apply_splices_text():
    op = TextOperation().retain(5).insert(" there").retain(6)
    assert op.apply("hello world") == "hello there world"


def test_apply_rejects_a_mismatched_document():
    op = TextOperation().retain(3)
    with pytest.raises(OTError):
        op.apply("much longer document")


def test_operations_are_canonical():
    op = TextOperation().retain(2).retain(3).insert("a").insert("b")
    assert op.ops == [5, "ab"]


def test_insert_is_ordered_ahead_of_delete():
    # Two ways of writing the same edit must produce the same components, or
    # equality checks and the wire format stop being meaningful.
    a = TextOperation().delete(3).insert("x")
    b = TextOperation().insert("x").delete(3)
    assert a == b


def test_concurrent_inserts_converge():
    doc = "hello"
    a = TextOperation().retain(5).insert(" alice")
    b = TextOperation().retain(5).insert(" bob")
    a_prime, b_prime = TextOperation.transform(a, b)
    assert b_prime.apply(a.apply(doc)) == a_prime.apply(b.apply(doc))


def test_insert_into_a_range_the_other_user_deleted():
    doc = "abcdef"
    a = TextOperation().retain(2).insert("XY").retain(4)
    b = TextOperation().retain(1).delete(4).retain(1)
    a_prime, b_prime = TextOperation.transform(a, b)
    converged = b_prime.apply(a.apply(doc))
    assert converged == a_prime.apply(b.apply(doc))
    assert converged == "aXYf"


def test_overlapping_deletes_are_not_applied_twice():
    doc = "abcdefgh"
    a = TextOperation().retain(2).delete(3).retain(3)
    b = TextOperation().retain(3).delete(3).retain(2)
    a_prime, b_prime = TextOperation.transform(a, b)
    assert b_prime.apply(a.apply(doc)) == "abgh"
    assert a_prime.apply(b.apply(doc)) == "abgh"


def test_transform_requires_a_shared_base():
    with pytest.raises(OTError):
        TextOperation.transform(
            TextOperation().retain(3), TextOperation().retain(5)
        )


def test_compose_matches_sequential_application():
    doc = "the quick brown fox"
    a = TextOperation().retain(4).delete(6).insert("slow ").retain(9)
    b = TextOperation().retain(0).insert(">> ").retain(18)
    assert TextOperation.compose(a, b).apply(doc) == b.apply(a.apply(doc))


def test_invert_restores_the_original():
    doc = "collaborative editing"
    op = TextOperation().retain(13).delete(8).insert("documents")
    assert op.invert(doc).apply(op.apply(doc)) == doc


def test_from_diff_recovers_a_single_splice():
    op = TextOperation.from_diff("the quick fox", "the very quick red fox")
    assert op.apply("the quick fox") == "the very quick red fox"


def test_from_diff_on_identical_text_is_a_noop():
    assert TextOperation.from_diff("same", "same").is_noop()


def test_json_round_trip():
    op = TextOperation().retain(3).delete(2).insert("hi")
    assert TextOperation.from_json(op.to_json_str()) == op


@pytest.mark.parametrize(
    "position,expected",
    [(0, 0), (3, 3), (5, 9), (11, 15)],
)
def test_cursor_follows_an_insert(position, expected):
    op = TextOperation().retain(4).insert("XXXX").retain(7)
    assert op.transform_cursor(position) == expected


def test_cursor_collapses_into_a_deleted_range():
    op = TextOperation().retain(2).delete(5).retain(3)
    assert op.transform_cursor(4) == 2
    assert op.transform_cursor(9) == 4


def _random_operation(doc: str, rng: random.Random) -> TextOperation:
    op = TextOperation()
    i = 0
    while i < len(doc):
        roll = rng.random()
        span = min(len(doc) - i, rng.randint(1, 4))
        if roll < 0.4:
            op.retain(span)
            i += span
        elif roll < 0.7:
            op.delete(span)
            i += span
        else:
            op.insert("".join(rng.choice("abc ") for _ in range(rng.randint(1, 3))))
    if rng.random() < 0.5:
        op.insert("!")
    return op


def test_transform_converges_under_random_edits():
    """The convergence property, checked against 2000 random edit pairs."""
    rng = random.Random(20240514)
    for _ in range(2000):
        doc = "".join(rng.choice("abcdef ") for _ in range(rng.randint(0, 30)))
        a = _random_operation(doc, rng)
        b = _random_operation(doc, rng)
        a_prime, b_prime = TextOperation.transform(a, b)
        assert b_prime.apply(a.apply(doc)) == a_prime.apply(b.apply(doc))


def test_compose_and_invert_under_random_edits():
    rng = random.Random(99)
    for _ in range(1000):
        doc = "".join(rng.choice("xyz ") for _ in range(rng.randint(0, 25)))
        a = _random_operation(doc, rng)
        b = _random_operation(a.apply(doc), rng)
        assert TextOperation.compose(a, b).apply(doc) == b.apply(a.apply(doc))
        assert a.invert(doc).apply(a.apply(doc)) == doc
