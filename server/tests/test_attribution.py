"""AI authorship attribution (WBS 9.3, covers WBS 6.1-6.3).

The headline assertion is the milestone from the WBS dictionary: the
stylometric classifier must reach at least 85% accuracy on a held-out test
set.
"""
from __future__ import annotations

import random

import corpora
import pytest

from app.attribution import blocks as block_parser
from app.attribution import features
from app.attribution.classifier import StylometricClassifier
from app.attribution.engine import AttributionEngine
from app.attribution.trail import AuthorTrail
from app.models import BlockType
from app.realtime.ot import TextOperation

ACCURACY_FLOOR = 0.85  # WBS 9.3 milestone


# -- feature extraction (WBS 6.1) ----------------------------------------


def test_empty_text_gives_a_zero_vector():
    assert features.extract("") == [0.0] * features.FEATURE_COUNT
    assert features.extract("   \n ") == [0.0] * features.FEATURE_COUNT


def test_every_feature_is_produced():
    vector = features.extract("The cat sat. It didn't move; nobody knew why!")
    assert len(vector) == features.FEATURE_COUNT == len(features.FEATURE_NAMES)
    assert all(isinstance(value, float) for value in vector)


def test_features_are_scale_free():
    """One paragraph and three copies of it must look like the same author."""
    once = features.extract(corpora.academic(random.Random(1), sentences=2))
    thrice = features.extract(corpora.academic(random.Random(1), sentences=6))
    named_once = features.describe(once)
    named_thrice = features.describe(thrice)
    for name in ("mean_word_length", "mean_sentence_words", "punct_,"):
        assert named_once[name] == pytest.approx(named_thrice[name], rel=0.15)


def test_different_styles_separate_on_the_obvious_features():
    formal = features.describe(features.extract(corpora.academic(random.Random(2))))
    casual = features.describe(features.extract(corpora.blogger(random.Random(2))))
    assert formal["mean_sentence_words"] > casual["mean_sentence_words"]
    assert casual["contraction_rate"] > formal["contraction_rate"]


def test_sentence_splitting():
    assert features.sentence_lengths("One two. Three four five! Six?") == [2, 3, 1]


# -- classifier (WBS 6.2) -------------------------------------------------


def _dataset(styles, samples, seed):
    corpus = corpora.build(styles, samples, seed=seed)
    vectors, labels = [], []
    for name, texts in corpus.items():
        for text in texts:
            vectors.append(features.extract(text))
            labels.append(name)
    return vectors, labels


def test_classifier_meets_the_accuracy_floor_on_held_out_text():
    train_x, train_y = _dataset(["academic", "blogger", "analyst"], 40, seed=11)
    test_x, test_y = _dataset(["academic", "blogger", "analyst"], 20, seed=97)

    model = StylometricClassifier().fit(train_x, train_y)
    accuracy = model.score(test_x, test_y)
    assert accuracy >= ACCURACY_FLOOR, "held-out accuracy was %.3f" % accuracy


def test_two_author_discrimination():
    train_x, train_y = _dataset(["academic", "blogger"], 40, seed=5)
    test_x, test_y = _dataset(["academic", "blogger"], 20, seed=6)
    assert StylometricClassifier().fit(train_x, train_y).score(test_x, test_y) >= ACCURACY_FLOOR


def test_probabilities_are_a_distribution():
    train_x, train_y = _dataset(["academic", "blogger"], 20, seed=3)
    model = StylometricClassifier().fit(train_x, train_y)
    probabilities = model.predict_proba(features.extract(corpora.blogger(random.Random(8))))
    assert set(probabilities) == {"academic", "blogger"}
    assert sum(probabilities.values()) == pytest.approx(1.0)
    assert all(0.0 <= p <= 1.0 for p in probabilities.values())


def test_a_single_author_is_predicted_with_certainty():
    train_x, train_y = _dataset(["academic"], 10, seed=3)
    model = StylometricClassifier().fit(train_x, train_y)
    assert model.predict(train_x[0]) == ("academic", 1.0)


def test_model_survives_serialisation():
    train_x, train_y = _dataset(["academic", "blogger"], 20, seed=4)
    model = StylometricClassifier().fit(train_x, train_y)
    restored = StylometricClassifier.from_json(model.to_json())
    sample = features.extract(corpora.analyst(random.Random(9)))
    assert restored.predict_proba(sample) == model.predict_proba(sample)


def test_training_on_nothing_is_an_error():
    with pytest.raises(ValueError):
        StylometricClassifier().fit([], [])


# -- block parsing (SRS 6.5) ---------------------------------------------


def test_blocks_are_typed_and_located():
    document = "# Title\n\nA paragraph here.\n- first item\n2. second item\n"
    parsed = block_parser.parse(document)
    assert [b.block_type for b in parsed] == [
        BlockType.HEADING,
        BlockType.PARAGRAPH,
        BlockType.LIST_ITEM,
        BlockType.LIST_ITEM,
    ]
    assert [document[b.start : b.end] for b in parsed] == [
        "# Title",
        "A paragraph here.",
        "- first item",
        "2. second item",
    ]


def test_block_text_strips_its_markup():
    parsed = block_parser.parse("## Heading here\n- list entry\n")
    assert parsed[0].text == "Heading here"
    assert parsed[1].text == "list entry"


def test_sort_order_leaves_room_for_inserts():
    parsed = block_parser.parse("one\ntwo\n")
    between = block_parser.rank_between(parsed[0].sort_order, parsed[1].sort_order)
    assert parsed[0].sort_order < between < parsed[1].sort_order


# -- authorship ledger ----------------------------------------------------


def test_ledger_tracks_who_typed_what():
    trail = AuthorTrail()
    trail.apply(TextOperation().insert("hello"), "alice")
    trail.apply(TextOperation().retain(5).insert(" world"), "bob")
    assert trail.length == 11
    assert trail.owner_at(0) == "alice"
    assert trail.owner_at(7) == "bob"
    assert trail.share_of_range(0, 11) == {"alice": 5 / 11, "bob": 6 / 11}


def test_ledger_follows_a_deletion():
    trail = AuthorTrail()
    trail.apply(TextOperation().insert("abcdef"), "alice")
    trail.apply(TextOperation().retain(2).delete(2).retain(2), "bob")
    assert trail.length == 4
    assert [run[1] for run in trail.to_json()] == ["alice"]


def test_ledger_attributes_a_replacement_to_the_replacer():
    trail = AuthorTrail()
    trail.apply(TextOperation().insert("the quick fox"), "alice")
    trail.apply(TextOperation().retain(4).delete(5).insert("slow").retain(4), "bob")
    user, share = trail.dominant_of_range(4, 8)
    assert user == "bob" and share == 1.0


def test_ledger_splits_the_document_by_author():
    trail = AuthorTrail()
    trail.apply(TextOperation().insert("alice wrote this"), "alice")
    trail.apply(TextOperation().retain(16).insert(" and bob added this"), "bob")
    chunks = trail.text_by_author("alice wrote this and bob added this")
    assert chunks["alice"] == ["alice wrote this"]
    assert chunks["bob"] == [" and bob added this"]


def test_ledger_round_trips_through_json():
    trail = AuthorTrail()
    trail.apply(TextOperation().insert("hello"), "alice")
    assert AuthorTrail.from_json(trail.to_json()).to_json() == trail.to_json()


# -- the engine end to end (WBS 6.3) --------------------------------------


def _document_typed_by_two_authors(seed: int = 17):
    """Build a document where each author's blocks are in their own style."""
    rng = random.Random(seed)
    trail = AuthorTrail()
    document = ""
    for index in range(8):
        author, text = (
            ("alice", corpora.academic(rng, sentences=2))
            if index % 2 == 0
            else ("bob", corpora.blogger(rng, sentences=3))
        )
        line = text + "\n"
        operation = TextOperation().retain(len(document)).insert(line)
        document = operation.apply(document)
        trail.apply(operation, author)
    return document, trail


def test_engine_attributes_blocks_to_the_right_author():
    document, trail = _document_typed_by_two_authors()
    engine = AttributionEngine(min_chars=80, min_samples_per_author=3)
    report = engine.train(trail.text_by_author(document))
    assert report.trained
    assert sorted(report.authors) == ["alice", "bob"]

    results = engine.attribute_document(document, trail)
    assert len(results) == 8
    assert all(block.method == "hybrid" for block in results)
    correct = sum(
        1
        for index, block in enumerate(results)
        if block.attributed_user_id == ("alice" if index % 2 == 0 else "bob")
    )
    assert correct == len(results)


def test_style_alone_identifies_the_author_without_the_ledger():
    """The stylometric half must stand on its own, not lean on the ledger."""
    document, trail = _document_typed_by_two_authors()
    engine = AttributionEngine(min_chars=80, min_samples_per_author=3)
    engine.train(trail.text_by_author(document))

    results = engine.attribute_document(document, trail)
    agree = sum(1 for b in results if b.style_user_id == b.ledger_user_id)
    assert agree / len(results) >= ACCURACY_FLOOR


def test_engine_falls_back_to_the_ledger_with_one_author():
    trail = AuthorTrail()
    text = "A single author wrote every word of this document.\n"
    trail.apply(TextOperation().insert(text), "alice")

    engine = AttributionEngine(min_chars=80, min_samples_per_author=3)
    report = engine.train(trail.text_by_author(text))
    assert not report.trained
    assert "keystroke ledger" in report.reason

    results = engine.attribute_document(text, trail)
    assert results[0].method == "ledger"
    assert results[0].attributed_user_id == "alice"
    assert results[0].confidence_score == 1.0


def test_short_blocks_lean_on_the_ledger():
    """A heading is too short to judge stylistically, so keystrokes decide."""
    document, trail = _document_typed_by_two_authors()
    heading = "# Hi\n"
    operation = TextOperation().insert(heading).retain(len(document))
    document = operation.apply(document)
    trail.apply(operation, "bob")

    engine = AttributionEngine(min_chars=120, min_samples_per_author=3)
    engine.train(trail.text_by_author(document))
    results = engine.attribute_document(document, trail)
    assert results[0].content == "# Hi"
    assert results[0].attributed_user_id == "bob"


def test_chunking_produces_usable_training_samples():
    engine = AttributionEngine(min_chars=100)
    text = corpora.academic(random.Random(3), sentences=12)
    samples = engine.chunk(text)
    assert len(samples) >= 3
    assert all(len(sample) >= 50 for sample in samples)
    assert "".join(samples).replace(" ", "") == text.replace(" ", "")


# -- the HTTP surface -----------------------------------------------------


def test_attribution_endpoint_reports_blocks_and_model(alice):
    document_id = alice.new_document(
        "Styles", content="# Heading\n\nA paragraph of prose here.\n"
    )
    payload = alice.get("/api/documents/%s/attribution" % document_id).json()
    assert [b["block_type"] for b in payload["blocks"]] == ["HEADING", "PARAGRAPH"]
    assert all(b["attributed_user_id"] == alice.id for b in payload["blocks"])
    assert payload["model"]["trained"] is False  # only one author so far


def test_metrics_endpoint_summarises_the_pass(alice, bob):
    document_id = alice.new_document("Styles", content="A paragraph of prose here.\n")
    alice.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": bob.email, "role": "EDITOR"},
    )
    metrics = alice.get("/api/documents/%s/attribution/metrics" % document_id).json()
    assert metrics["block_count"] == 1
    assert 0.0 <= metrics["mean_confidence"] <= 1.0
    assert alice.id in metrics["per_author"]


def test_a_writing_sample_can_be_contributed(alice):
    document_id = alice.new_document("Styles", content="short\n")
    response = alice.post(
        "/api/documents/%s/attribution/samples" % document_id,
        json={"content": corpora.academic(random.Random(1), sentences=6)},
    )
    assert response.status_code == 201
    assert response.json()["stored"] == 1


def test_a_viewer_cannot_force_a_retrain(alice, bob):
    document_id = alice.new_document("Styles", content="text\n")
    alice.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": bob.email, "role": "VIEWER"},
    )
    assert (
        bob.post("/api/documents/%s/attribution/refresh" % document_id).status_code == 403
    )
    assert bob.get("/api/documents/%s/attribution" % document_id).status_code == 200
