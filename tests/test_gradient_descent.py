import math
import random
import unittest

from core import gradient_descent as gd


def _dataset(
    n=600,
    true_weights=(0.60, 0.25, 0.15),
    seed=42,
    binary=False,
    bias=0.0,
    scale=None,
):
    """Jeu synthétique dont les labels sont tirés du *vrai* modèle linéaire.

    ``binary`` donne des features {0,1} (variance plus forte donc signal plus
    identifiable) ; ``scale`` applique une température autour de 0.5 pour rendre
    les classes plus séparables (cas de la régression logistique libre, qui peut
    absorber l'échelle via la norme de ses poids).
    """
    rng = random.Random(seed)
    X, y = [], []
    for _ in range(n):
        if binary:
            x = [1.0 if rng.random() < 0.5 else 0.0 for _ in range(3)]
        else:
            x = [rng.random(), rng.random(), rng.random()]
        if scale is not None:
            z = scale * (gd.dot(true_weights, x) - 0.5)
        else:
            z = gd.dot(true_weights, x) + bias
        p = gd.sigmoid(z)
        X.append(x)
        y.append(1.0 if rng.random() < p else 0.0)
    return X, y


class TestPrimitives(unittest.TestCase):
    def test_sigmoid_bounds_and_monotonic(self):
        self.assertAlmostEqual(gd.sigmoid(0.0), 0.5)
        self.assertGreater(gd.sigmoid(1.0), 0.5)
        self.assertLess(gd.sigmoid(-1.0), 0.5)
        for z in (-1000.0, -30.0, 0.0, 30.0, 1000.0):
            self.assertGreaterEqual(gd.sigmoid(z), 0.0)
            self.assertLessEqual(gd.sigmoid(z), 1.0)
        self.assertAlmostEqual(gd.sigmoid(1000.0), 1.0)
        self.assertAlmostEqual(gd.sigmoid(-1000.0), 0.0)

    def test_softmax_sums_to_one(self):
        w = gd.softmax([0.0, 1.0, -2.0, 5.0])
        self.assertAlmostEqual(sum(w), 1.0, places=12)
        self.assertTrue(all(0.0 < wi < 1.0 for wi in w))
        # Ordre préservé
        self.assertGreater(w[3], w[1])
        self.assertGreater(w[1], w[0])
        self.assertGreater(w[0], w[2])

    def test_softmax_logits_roundtrip(self):
        weights = [0.40, 0.30, 0.30]
        self.assertEqual(len(gd.logits_from_weights(weights)), 3)
        back = gd.softmax(gd.logits_from_weights(weights))
        for a, b in zip(back, weights):
            self.assertAlmostEqual(a, b, places=10)

    def test_binary_cross_entropy_is_finite_at_extremes(self):
        for p in (0.0, 1.0):
            for y in (0.0, 1.0):
                self.assertTrue(math.isfinite(gd.binary_cross_entropy(p, y)))
        self.assertAlmostEqual(gd.binary_cross_entropy(0.5, 1.0), math.log(2.0))


class TestGradientCorrectness(unittest.TestCase):
    """Preuve que le gradient analytique == gradient numérique (différences finies)."""

    def test_logistic_gradient_matches_numerical(self):
        X, y = _dataset(n=60, seed=7)
        weights = [0.3, -0.2, 0.5]
        bias = 0.1

        def f(params):
            return gd.logistic_loss(X, y, params[:3], params[3])

        analytic_w, analytic_b = gd.logistic_gradients(X, y, weights, bias)
        numeric = gd.numerical_gradient(f, weights + [bias])

        for a, n in zip(analytic_w + [analytic_b], numeric):
            self.assertAlmostEqual(a, n, places=6)

    def test_simplex_gradient_matches_numerical(self):
        X, y = _dataset(n=60, seed=11)
        theta = gd.logits_from_weights([0.5, 0.3, 0.2])
        bias = -0.05

        def f(params):
            return gd.simplex_loss(X, y, params[:3], params[3])

        analytic_t, analytic_b = gd.simplex_gradients(X, y, theta, bias)
        numeric = gd.numerical_gradient(f, theta + [bias])

        for a, n in zip(analytic_t + [analytic_b], numeric):
            self.assertAlmostEqual(a, n, places=6)


class TestConvergence(unittest.TestCase):
    def test_fit_binary_logistic_reduces_loss_and_separates(self):
        true_weights = (0.60, 0.25, 0.15)
        X, y = _dataset(true_weights=true_weights, seed=3, scale=10.0)
        fit = gd.fit_binary_logistic(X, y, learning_rate=0.5, epochs=300, l2=0.0)
        self.assertLess(fit.loss, fit.loss_history[0])
        correct = sum(
            1 for xi, yi in zip(X, y) if (fit.predict(xi) >= 0.5) == (yi == 1.0)
        )
        self.assertGreater(correct / len(y), 0.7)
        # La régression libre doit retrouver l'ORDRE d'importance des features
        # (elle est identifiée à l'échelle près, pas à une constante près).
        self.assertGreater(fit.weights[0], fit.weights[1])
        self.assertGreater(fit.weights[1], fit.weights[2])

    def test_simplex_weights_stay_on_simplex_and_learn_ordering(self):
        # Features binaires + biais négatif : rend l'ordre des poids identifiable
        # sans exiger une précision numérique irréaliste.
        X, y = _dataset(seed=42, binary=True, bias=-0.35)
        fit = gd.fit_simplex_weights(
            X, y, prior_weights=(0.40, 0.30, 0.30), learning_rate=0.8, epochs=300, l2=1e-4
        )
        self.assertAlmostEqual(sum(fit.weights), 1.0, places=9)
        self.assertTrue(all(0.0 < w < 1.0 for w in fit.weights))
        self.assertLess(fit.loss, fit.loss_history[0])
        # La feature la plus informative (index 0) doit dominer.
        self.assertGreater(fit.weights[0], fit.weights[1])
        self.assertGreater(fit.weights[1], fit.weights[2])

    def test_regularization_keeps_weights_near_prior_without_signal(self):
        """Labels aléatoires (aucun signal) => L2 doit retenir le modèle près du prior."""
        rng = random.Random(99)
        X = [[rng.random(), rng.random(), rng.random()] for _ in range(400)]
        y = [1.0 if rng.random() < 0.5 else 0.0 for _ in range(400)]
        prior = (0.40, 0.30, 0.30)
        fit = gd.fit_simplex_weights(
            X, y, prior_weights=prior, learning_rate=0.5, epochs=500, l2=0.5
        )
        for got, expected in zip(fit.weights, prior):
            self.assertLess(abs(got - expected), 0.25)

    def test_fit_is_deterministic(self):
        X, y = _dataset(n=100, seed=5)
        a = gd.fit_simplex_weights(X, y)
        b = gd.fit_simplex_weights(X, y)
        self.assertEqual(a.weights, b.weights)
        self.assertEqual(a.bias, b.bias)

    def test_empty_inputs_are_safe(self):
        fit = gd.fit_simplex_weights([], [])
        self.assertEqual(fit.weights, [])
        self.assertEqual(fit.loss_history, [])

    def test_brier_score_bounds(self):
        self.assertAlmostEqual(gd.brier_score([1.0, 0.0], [1.0, 0.0]), 0.0)
        self.assertAlmostEqual(gd.brier_score([0.0, 1.0], [1.0, 0.0]), 1.0)


if __name__ == "__main__":
    unittest.main()
