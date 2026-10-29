"""Lightweight stylometric classifier (WBS 6.2).

A multinomial logistic regression (softmax) trained by full-batch gradient
descent with L2 regularisation, written against plain Python lists.

Why not a heavier library: the model has ~44 features and a handful of classes
(the collaborators on one document), so a fitted model is a few hundred floats
and training takes milliseconds. Keeping it dependency-free means the
attribution service can be trained inline on the request path and pickled into
a row rather than shipped to a separate model server -- which is what lets
WBS 6.3 hit the near-real-time budget in SRS 5.1.

The classifier reports calibrated probabilities, and the top probability is the
``confidence_score`` persisted on ``document_blocks`` (SRS 6.5).
"""
from __future__ import annotations

import json
import math

Vector = list[float]


class StylometricClassifier:
    """Softmax regression over standardised stylometric features."""

    def __init__(
        self,
        learning_rate: float = 0.35,
        epochs: int = 400,
        l2: float = 0.02,
        seed: int = 1729,
    ) -> None:
        self.learning_rate = learning_rate
        self.epochs = epochs
        self.l2 = l2
        self.seed = seed
        self.classes: list[str] = []
        self.weights: list[Vector] = []   # one row per class
        self.biases: Vector = []
        self.mean: Vector = []
        self.scale: Vector = []
        self.n_features = 0
        self.trained = False
        self.training_size = 0

    # -- standardisation ---------------------------------------------------

    def _fit_scaler(self, samples: list[Vector]) -> None:
        n = len(samples)
        d = len(samples[0])
        self.mean = [sum(s[j] for s in samples) / n for j in range(d)]
        scale = []
        for j in range(d):
            var = sum((s[j] - self.mean[j]) ** 2 for s in samples) / max(1, n - 1)
            sd = math.sqrt(var)
            # A feature that never varies carries no signal; a unit scale keeps
            # it at zero rather than dividing by (almost) nothing.
            scale.append(sd if sd > 1e-9 else 1.0)
        self.scale = scale

    def _standardise(self, vector: Vector) -> Vector:
        return [
            (vector[j] - self.mean[j]) / self.scale[j] for j in range(self.n_features)
        ]

    # -- training ----------------------------------------------------------

    def fit(self, samples: list[Vector], labels: list[str]) -> "StylometricClassifier":
        if not samples:
            raise ValueError("cannot train on an empty corpus")
        if len(samples) != len(labels):
            raise ValueError("samples and labels must be the same length")

        self.classes = sorted(set(labels))
        self.n_features = len(samples[0])
        self.training_size = len(samples)

        if len(self.classes) == 1:
            # One author: no decision to make, and gradient descent on a
            # single class would only learn the bias. Short-circuit it.
            self.weights = [[0.0] * self.n_features]
            self.biases = [0.0]
            self._fit_scaler(samples)
            self.trained = True
            return self

        self._fit_scaler(samples)
        x = [self._standardise(s) for s in samples]
        class_index = {c: i for i, c in enumerate(self.classes)}
        y = [class_index[label] for label in labels]

        k, d, n = len(self.classes), self.n_features, len(x)
        self.weights = [[0.0] * d for _ in range(k)]
        self.biases = [0.0] * k

        lr = self.learning_rate
        previous_loss = float("inf")
        for _ in range(self.epochs):
            grad_w = [[0.0] * d for _ in range(k)]
            grad_b = [0.0] * k
            loss = 0.0
            for i in range(n):
                probs = self._softmax(self._logits(x[i]))
                loss -= math.log(max(probs[y[i]], 1e-12))
                for c in range(k):
                    error = probs[c] - (1.0 if c == y[i] else 0.0)
                    if error == 0.0:
                        continue
                    row = grad_w[c]
                    xi = x[i]
                    for j in range(d):
                        row[j] += error * xi[j]
                    grad_b[c] += error
            loss = loss / n + self.l2 * sum(
                w * w for row in self.weights for w in row
            )
            for c in range(k):
                row, grow = self.weights[c], grad_w[c]
                for j in range(d):
                    row[j] -= lr * (grow[j] / n + 2 * self.l2 * row[j])
                self.biases[c] -= lr * grad_b[c] / n

            # Back off the step size when the loss stops improving, and stop
            # once it has converged -- this is the whole learning-rate schedule.
            if loss > previous_loss:
                lr *= 0.5
            elif previous_loss - loss < 1e-7:
                break
            previous_loss = loss

        self.trained = True
        return self

    # -- inference ---------------------------------------------------------

    def _logits(self, standardised: Vector) -> Vector:
        return [
            sum(w * v for w, v in zip(self.weights[c], standardised)) + self.biases[c]
            for c in range(len(self.classes))
        ]

    @staticmethod
    def _softmax(logits: Vector) -> Vector:
        peak = max(logits)
        exps = [math.exp(v - peak) for v in logits]
        total = sum(exps)
        return [e / total for e in exps]

    def predict_proba(self, vector: Vector) -> dict[str, float]:
        if not self.trained:
            raise RuntimeError("classifier has not been trained")
        if len(self.classes) == 1:
            return {self.classes[0]: 1.0}
        probs = self._softmax(self._logits(self._standardise(vector)))
        return dict(zip(self.classes, probs))

    def predict(self, vector: Vector) -> tuple[str, float]:
        """The most likely author and the probability behind that choice."""
        probs = self.predict_proba(vector)
        label = max(probs, key=probs.__getitem__)
        return label, probs[label]

    def score(self, samples: list[Vector], labels: list[str]) -> float:
        """Plain accuracy -- the metric WBS 9.3 holds to a 0.85 floor."""
        if not samples:
            return 0.0
        hits = sum(
            1 for s, label in zip(samples, labels) if self.predict(s)[0] == label
        )
        return hits / len(samples)

    # -- persistence -------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(
            {
                "classes": self.classes,
                "weights": self.weights,
                "biases": self.biases,
                "mean": self.mean,
                "scale": self.scale,
                "n_features": self.n_features,
                "training_size": self.training_size,
            }
        )

    @classmethod
    def from_json(cls, blob: str) -> "StylometricClassifier":
        data = json.loads(blob)
        model = cls()
        model.classes = data["classes"]
        model.weights = data["weights"]
        model.biases = data["biases"]
        model.mean = data["mean"]
        model.scale = data["scale"]
        model.n_features = data["n_features"]
        model.training_size = data.get("training_size", 0)
        model.trained = True
        return model
