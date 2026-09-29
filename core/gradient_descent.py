"""Descente de gradient minimale, en **Python pur** (aucune dépendance).

Pourquoi sans numpy ? `numpy` et `pandas` ont été retirés de `requirements.txt`
pour alléger le déploiement. Pour l'échelle visée ici — quelques paramètres
(les poids TA / sentiment / macro, voire un biais) — une implémentation en
listes Python est plus rapide à charger, déterministe et exactement testable.
Au-delà de quelques centaines de paramètres, il faudra réintroduire numpy.

Le module fournit :

* ``sigmoid`` / ``softmax`` / ``binary_cross_entropy`` (stables numériquement) ;
* ``logistic_gradients`` : gradient analytique d'une régression logistique ;
* ``simplex_gradients`` : gradient d'un modèle dont les poids sont **contraints
  au simplexe** (somme = 1) via une paramétrisation softmax — c'est exactement
  la contrainte des poids ``ta_weight`` + ``sentiment_weight`` + ``macro_weight`` ;
* ``fit_binary_logistic`` / ``fit_simplex_weights`` : descente de gradient
  plein-batch avec régularisation L2 vers un **a priori** (le prior
  ``0.40 / 0.30 / 0.30`` actuel) ;
* ``numerical_gradient`` : gradient par différences finies, utilisé par les
  tests pour *prouver* que le gradient analytique est correct.

Ce module est volontairement **pur et sans effet de bord** : il ne touche ni
Supabase, ni la base, ni le pipeline d'exécution.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

#: Clamp pour éviter ``log(0)`` dans l'entropie croisée.
_EPS = 1e-12


def sigmoid(z: float) -> float:
    """Sigmoïde stable (pas de dépassement pour les grands |z|)."""
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def softmax(logits: Sequence[float]) -> List[float]:
    """Softmax stable. Les valeurs retournées somment à 1."""
    if not logits:
        return []
    m = max(logits)
    exps = [math.exp(z - m) for z in logits]
    total = sum(exps)
    return [e / total for e in exps]


def logits_from_weights(weights: Sequence[float]) -> List[float]:
    """Réciproque de ``softmax`` : retrouve des logits à partir de poids."""
    return [math.log(max(w, _EPS)) for w in weights]


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def binary_cross_entropy(p: float, y: float) -> float:
    """Entropie croisée binaire, bornée pour ne jamais renvoyer +inf."""
    p = min(max(p, _EPS), 1.0 - _EPS)
    return -(y * math.log(p) + (1.0 - y) * math.log(1.0 - p))


def brier_score(probabilities: Sequence[float], labels: Sequence[float]) -> float:
    """Brier score moyen (plus bas = mieux calibré)."""
    if not probabilities:
        return 0.0
    return sum((p - y) ** 2 for p, y in zip(probabilities, labels)) / len(probabilities)


# --------------------------------------------------------------------------- #
# Modèle 1 : régression logistique « libre » (poids + biais)
# --------------------------------------------------------------------------- #


def logistic_predict(weights: Sequence[float], bias: float, x: Sequence[float]) -> float:
    return sigmoid(dot(weights, x) + bias)


def logistic_gradients(
    X: Sequence[Sequence[float]],
    y: Sequence[float],
    weights: Sequence[float],
    bias: float,
    l2: float = 0.0,
    prior_weights: Optional[Sequence[float]] = None,
) -> Tuple[List[float], float]:
    """Gradient de la perte BCE (+ L2) par rapport à ``weights`` et ``bias``."""
    n = len(X)
    if n == 0:
        return [0.0] * len(weights), 0.0

    grad_w = [0.0] * len(weights)
    grad_b = 0.0
    for xi, yi in zip(X, y):
        error = sigmoid(dot(weights, xi) + bias) - yi
        for j, xij in enumerate(xi):
            grad_w[j] += error * xij
        grad_b += error

    grad_w = [g / n for g in grad_w]
    grad_b /= n

    if l2 > 0.0 and prior_weights is not None:
        for j in range(len(grad_w)):
            grad_w[j] += l2 * (weights[j] - prior_weights[j])
    return grad_w, grad_b


def logistic_loss(
    X: Sequence[Sequence[float]],
    y: Sequence[float],
    weights: Sequence[float],
    bias: float,
    l2: float = 0.0,
    prior_weights: Optional[Sequence[float]] = None,
) -> float:
    n = len(X)
    if n == 0:
        return 0.0
    data_loss = sum(
        binary_cross_entropy(logistic_predict(weights, bias, xi), yi)
        for xi, yi in zip(X, y)
    ) / n
    if l2 > 0.0 and prior_weights is not None:
        data_loss += 0.5 * l2 * sum(
            (w - p) ** 2 for w, p in zip(weights, prior_weights)
        )
    return data_loss


@dataclass
class LogisticFit:
    weights: List[float]
    bias: float
    loss_history: List[float] = field(default_factory=list)

    @property
    def loss(self) -> float:
        return self.loss_history[-1] if self.loss_history else float("nan")

    def predict(self, x: Sequence[float]) -> float:
        return logistic_predict(self.weights, self.bias, x)

    def to_dict(self) -> dict:
        return {"weights": list(self.weights), "bias": self.bias, "loss": self.loss}


def fit_binary_logistic(
    X: Sequence[Sequence[float]],
    y: Sequence[float],
    learning_rate: float = 0.5,
    epochs: int = 500,
    l2: float = 1e-3,
    prior_weights: Optional[Sequence[float]] = None,
    initial_weights: Optional[Sequence[float]] = None,
) -> LogisticFit:
    """Descente de gradient plein-batch d'une régression logistique."""
    n_features = len(X[0]) if X else 0
    if n_features == 0 or not y:
        return LogisticFit(weights=[], bias=0.0, loss_history=[])

    weights = list(initial_weights or [0.0] * n_features)
    prior = list(prior_weights or [0.0] * n_features)
    bias = 0.0
    history: List[float] = []

    for _ in range(epochs):
        history.append(logistic_loss(X, y, weights, bias, l2, prior))
        grad_w, grad_b = logistic_gradients(X, y, weights, bias, l2, prior)
        weights = [w - learning_rate * g for w, g in zip(weights, grad_w)]
        bias -= learning_rate * grad_b

    history.append(logistic_loss(X, y, weights, bias, l2, prior))
    return LogisticFit(weights=weights, bias=bias, loss_history=history)


# --------------------------------------------------------------------------- #
# Modèle 2 : poids contraints au simplexe (TA / sentiment / macro)
# --------------------------------------------------------------------------- #


def simplex_score(theta: Sequence[float], x: Sequence[float]) -> float:
    """Score signé dans (-1, 1) obtenu par combinaison convexe des sous-scores.

    ``x`` contient des sous-scores dans [0, 1] (TA, sentiment, macro, biais
    géopolitique...). Le score est ramené dans (-1, 1) — la même convention que
    ``core/macro_engine.py`` — puis passé à la sigmoïde pour obtenir une
    probabilité de succès.
    """
    w = softmax(theta)
    return 2.0 * dot(w, x) - 1.0


def simplex_probability(
    theta: Sequence[float], bias: float, x: Sequence[float]
) -> float:
    return sigmoid(dot(softmax(theta), x) + bias)


def simplex_gradients(
    X: Sequence[Sequence[float]],
    y: Sequence[float],
    theta: Sequence[float],
    bias: float,
    l2: float = 0.0,
    prior_theta: Optional[Sequence[float]] = None,
) -> Tuple[List[float], float]:
    """Gradient par rapport aux **logits** ``theta`` (poids = softmax(theta)).

    Avec ``p = sigmoid(w·x + b)`` et ``w = softmax(theta)`` :

        dL/dz  = p - y
        dL/dw_i = dL/dz * x_i
        dL/dtheta_j = w_j * (g_j - Σ_i g_i w_i),  g_i = dL/dz * x_i

    ce qui garantit que le gradient reste cohérent avec la contrainte de somme 1.
    """
    n = len(X)
    k = len(theta)
    if n == 0:
        return [0.0] * k, 0.0

    # `w` est calculé UNE seule fois hors boucle (softmax(theta) par échantillon
    # dominait le coût de calcul).
    w = softmax(theta)
    grad_theta = [0.0] * k
    grad_b = 0.0
    for xi, yi in zip(X, y):
        error = sigmoid(dot(w, xi) + bias) - yi
        error_terms = [error * xij for xij in xi]
        weighted = dot(error_terms, w)
        for j in range(k):
            grad_theta[j] += w[j] * (error_terms[j] - weighted)
        grad_b += error

    grad_theta = [g / n for g in grad_theta]
    grad_b /= n

    if l2 > 0.0 and prior_theta is not None:
        for j in range(k):
            grad_theta[j] += l2 * (theta[j] - prior_theta[j])
    return grad_theta, grad_b


def simplex_loss(
    X: Sequence[Sequence[float]],
    y: Sequence[float],
    theta: Sequence[float],
    bias: float,
    l2: float = 0.0,
    prior_theta: Optional[Sequence[float]] = None,
) -> float:
    n = len(X)
    if n == 0:
        return 0.0
    w = softmax(theta)
    data_loss = sum(
        binary_cross_entropy(sigmoid(dot(w, xi) + bias), yi)
        for xi, yi in zip(X, y)
    ) / n
    if l2 > 0.0 and prior_theta is not None:
        data_loss += 0.5 * l2 * sum(
            (t - p) ** 2 for t, p in zip(theta, prior_theta)
        )
    return data_loss


def fit_simplex_weights(
    X: Sequence[Sequence[float]],
    y: Sequence[float],
    prior_weights: Sequence[float] = (0.40, 0.30, 0.30),
    learning_rate: float = 0.5,
    epochs: int = 500,
    l2: float = 1e-3,
) -> LogisticFit:
    """Apprend les poids (somme = 1) par descente de gradient.

    ``prior_weights`` sert à la fois de point de départ et de cible de la
    régularisation L2 : avec peu de trades, le modèle reste proche de l'a priori
    au lieu de sur-réagir — c'est le garde-fou essentiel vu la rareté des labels.
    """
    n_features = len(X[0]) if X else 0
    if n_features == 0 or not y:
        return LogisticFit(weights=[], bias=0.0, loss_history=[])

    prior = list(prior_weights)
    prior_theta = logits_from_weights(prior)
    theta = list(prior_theta)
    bias = 0.0
    history: List[float] = []

    for _ in range(epochs):
        history.append(simplex_loss(X, y, theta, bias, l2, prior_theta))
        grad_t, grad_b = simplex_gradients(X, y, theta, bias, l2, prior_theta)
        theta = [t - learning_rate * g for t, g in zip(theta, grad_t)]
        bias -= learning_rate * grad_b

    history.append(simplex_loss(X, y, theta, bias, l2, prior_theta))
    return LogisticFit(weights=softmax(theta), bias=bias, loss_history=history)


# --------------------------------------------------------------------------- #
# Vérification du gradient
# --------------------------------------------------------------------------- #


def numerical_gradient(
    f: Callable[[Sequence[float]], float],
    params: Sequence[float],
    eps: float = 1e-6,
) -> List[float]:
    """Gradient par différences finies centrées — sert à valider le gradient analytique."""
    grad: List[float] = []
    for i in range(len(params)):
        plus = list(params)
        minus = list(params)
        plus[i] += eps
        minus[i] -= eps
        grad.append((f(plus) - f(minus)) / (2.0 * eps))
    return grad


__all__ = [
    "LogisticFit",
    "binary_cross_entropy",
    "brier_score",
    "dot",
    "fit_binary_logistic",
    "fit_simplex_weights",
    "logistic_gradients",
    "logistic_loss",
    "logistic_predict",
    "logits_from_weights",
    "numerical_gradient",
    "sigmoid",
    "simplex_gradients",
    "simplex_loss",
    "simplex_probability",
    "simplex_score",
    "softmax",
]
