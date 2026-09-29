"""Tests de **propriété** du moteur d'alerte : ce qui doit tenir pour *toute* série.

Un exemple choisi à la main ne prouve que lui-même : il dit qu'à cet endroit-là,
ce jour-là, la valeur était bonne. Les deux propriétés vérifiées ici, elles,
valent pour toutes les séries énumérées — et ce sont celles dont dépend la sûreté
de l'alerte :

* l'**ATR ne peut pas être négatif**. La volatilité est une amplitude, et le
  signe n'est pas décoratif : c'est lui qui place le stop-loss **sous** le prix
  pour un achat et le take-profit au-dessus. Un ATR négatif ne lève rien, il
  inverserait les deux — et avant d'y arriver il fait rejeter les croisements
  (`volatility <= 0`), c'est-à-dire perdre des signaux sans le dire ;
* l'**EMA reste dans l'enveloppe des clôtures** qu'elle a vues. Elle n'est qu'une
  moyenne pondérée à poids positifs de somme 1 (une combinaison convexe) : en
  sortir signifie un coefficient faux — et c'est ce coefficient qui produit les
  croisements, donc les signaux.

L'énumération est **aléatoire mais à graine fixe**, plutôt qu'une bibliothèque de
property-based testing : le projet n'a rien à installer pour cela, et un échec
doit se rejouer **au bit près**. Chaque cas est identifié par son numéro, que le
message d'échec rappelle :

    ALERT_PROPERTY_CASE=137 python -m unittest tests.test_alert_engine_properties

`ALERT_PROPERTY_SEED` change la graine (pour élargir la recherche), et
`ALERT_PROPERTY_CASES` le nombre de séries.

Les chandelles produites sont **valides** (``low ≤ min(open, close)`` et
``max(open, close) ≤ high``) parce que c'est ce que le marché produit : une
propriété qui n'existerait que sur des données impossibles ne protégerait rien.
Le cas de la ligne mal formée a son test à lui, nommé pour ce qu'il est.
"""

from __future__ import annotations

import math
import os
import random
import unittest
from unittest import mock

from core import alert_engine as ae

#: Graine fixe : l'énumération est reproductible, et **elle ne bouge plus**. La
#: changer rendrait irrejouables les échecs d'hier, ce qui est exactement ce
#: qu'une énumération aléatoire doit garantir.
DEFAULT_SEED = 20260928
DEFAULT_CASES = 400
MAX_LENGTH = 160

#: Les formes que le marché produit vraiment. Une énumération qui ne tirerait
#: qu'une seule forme ne prouverait rien des autres : plat (ATR nul), tendance,
#: marche aléatoire, saut (gap), longue mèche (TR mené par `high`, pas par le
#: corps), penny (valeurs minuscules), série constante.
REGIMES = ("plat", "hausse", "baisse", "marche", "saut", "meche", "penny", "constant")

#: Les périodes d'ATR éprouvées, dont les valeurs par défaut du moteur.
PERIODS = (1, 2, 5, 14, 21, 50, 200)

#: Les `span` d'EMA éprouvées, dont `EMA_FAST` et `EMA_SLOW`.
SPANS = (1, 2, 5, 14, 20, 50, 200)


def seed() -> int:
    """La graine de l'exécution : fixe par défaut, surchargeable pour élargir."""
    return int(os.environ.get("ALERT_PROPERTY_SEED", DEFAULT_SEED))


def selected_cases() -> range:
    """Les numéros de cas à énumérer — un seul si l'on rejoue un échec."""
    single = os.environ.get("ALERT_PROPERTY_CASE")
    if single is not None:
        return range(int(single), int(single) + 1)
    return range(int(os.environ.get("ALERT_PROPERTY_CASES", DEFAULT_CASES)))


def case_closes(index: int, *, seed_value: int | None = None) -> list:
    """Les clôtures du cas n° `index` — déterministes, quelle que soit la machine.

    La graine dérivée du numéro (une **chaîne**, dont Python tire la racine par
    SHA-512) est ce qui rend le cas indépendant de l'ordre d'exécution et de la
    version de l'interpréteur : `random.Random(index)` en dur suffirait, mais
    nommer la graine et le cas rend le rejeu lisible.
    """
    rng = random.Random(f"{DEFAULT_SEED if seed_value is None else seed_value}:{index}")
    regime = REGIMES[index % len(REGIMES)]
    price = rng.choice([1.0, 42.5, 1900.0, 0.02])
    # Les trois premiers cas portent les longueurs limites (1, 2 et 3 chandelles) :
    # les confier au hasard reviendrait à espérer qu'il les produise, alors que
    # c'est exactement là que les bornes se cassent (`len < span`, `len < period`).
    if index < 3:
        return [price] * (index + 1)
    length = rng.randint(1, MAX_LENGTH)
    if regime == "penny":
        price = rng.uniform(0.0001, 0.01)
    closes = [price]
    for _ in range(length - 1):
        if regime == "plat":
            step = 0.0
        elif regime == "constant":
            step = 0.0
        elif regime == "hausse":
            step = abs(rng.gauss(0.0, price * 0.01))
        elif regime == "baisse":
            step = -abs(rng.gauss(0.0, price * 0.01))
        elif regime == "saut":
            step = rng.choice([0.0, 0.0, 0.0, rng.gauss(0.0, price * 0.08)])
        elif regime == "marche":
            step = rng.gauss(0.0, price * 0.02)
        else:  # meche : le corps bouge peu, les extrêmes beaucoup (voir `case_series`)
            step = rng.gauss(0.0, price * 0.002)
        closes.append(max(closes[-1] + step, price * 0.01))
    return closes


def case_series(index: int, *, seed_value: int | None = None) -> list:
    """Les chandelles du cas n° `index` : OHLC valides, mèches comprises."""
    rng = random.Random(f"candles:{DEFAULT_SEED if seed_value is None else seed_value}:{index}")
    regime = REGIMES[index % len(REGIMES)]
    closes = case_closes(index, seed_value=seed_value)
    candles = []
    previous = closes[0]
    for close in closes:
        opened = previous if rng.random() < 0.6 else close
        body_low, body_high = min(opened, close), max(opened, close)
        if regime == "plat":
            low = high = close
        elif regime == "meche":
            # Une mèche franche : le true range est mené par `high`/`low`, pas par
            # le corps — c'est précisément ce qu'un ATR doit mesurer.
            low = body_low - abs(rng.gauss(0.0, max(body_high, 1e-9) * 0.05))
            high = body_high + abs(rng.gauss(0.0, max(body_high, 1e-9) * 0.05))
        else:
            low = body_low - abs(rng.gauss(0.0, max(body_high, 1e-9) * 0.002))
            high = body_high + abs(rng.gauss(0.0, max(body_high, 1e-9) * 0.002))
        low = max(low, 1e-9)
        high = max(high, low)
        candles.append({"open": opened, "high": high, "low": low, "close": close})
        previous = close
    return candles


def flat_candles(closes) -> list:
    """Chandelles dégénérées ``high = low = close`` : l'ATR y vaut l'amplitude close-à-close."""
    return [
        {"open": close, "high": close, "low": close, "close": close} for close in closes
    ]


class EnumerationTest(unittest.TestCase):
    """L'énumération elle-même : sans cela, les propriétés ci-dessous ne prouvent rien."""

    def test_the_same_seed_gives_the_same_series(self) -> None:
        """Une graine fixe qui ne serait pas fixe ne rejouerait aucun échec."""
        first = [case_series(index) for index in range(20)]
        second = [case_series(index) for index in range(20)]
        self.assertEqual(first, second)
        self.assertEqual(case_closes(3), case_closes(3))

    def test_the_series_are_only_made_of_valid_candles(self) -> None:
        """`low ≤ min(open, close)` et `max(open, close) ≤ high`, valeurs finies."""
        for index in selected_cases():
            for candle in case_series(index):
                for name, value in candle.items():
                    self.assertTrue(
                        math.isfinite(value) and value > 0,
                        f"cas {index} : {name}={value} n'est pas un prix",
                    )
                self.assertLessEqual(
                    candle["low"], min(candle["open"], candle["close"]), f"cas {index}"
                )
                self.assertGreaterEqual(
                    candle["high"], max(candle["open"], candle["close"]), f"cas {index}"
                )

    def test_every_regime_is_enumerated(self) -> None:
        """Sinon la propriété ne porterait que sur les formes qu'on a déjà vues."""
        seen = {REGIMES[index % len(REGIMES)] for index in range(len(REGIMES))}
        self.assertEqual(seen, set(REGIMES))

    def test_the_lengths_span_the_short_and_the_long(self) -> None:
        lengths = [len(case_closes(index)) for index in range(200)]
        self.assertTrue(
            {1, 2, 3} <= set(lengths), "les longueurs limites doivent être énumérées"
        )
        self.assertGreater(max(lengths), ae.EMA_SLOW + 1, "aucune série assez longue pour croiser")

    def test_a_single_case_can_be_replayed(self) -> None:
        """La recette de rejeu est vérifiée : un échec doit être reproductible seul."""
        with mock.patch.dict(os.environ, {"ALERT_PROPERTY_CASE": "137"}):
            self.assertEqual(list(selected_cases()), [137])

    def test_the_seed_can_be_changed_without_touching_the_default(self) -> None:
        self.assertNotEqual(case_closes(5), case_closes(5, seed_value=DEFAULT_SEED + 1))


class AtrIsNeverNegativeTest(unittest.TestCase):
    """La volatilité est une amplitude : son signe n'a pas de sens."""

    def test_over_every_enumerated_series(self) -> None:
        for index in selected_cases():
            series = case_series(index)
            for period in PERIODS:
                value = ae.atr(series, period)
                if value is None:
                    continue
                self.assertGreaterEqual(
                    value,
                    0.0,
                    f"cas {index} (période {period}) : ATR négatif — rejoue "
                    f"ALERT_PROPERTY_CASE={index}",
                )

    def test_the_true_ranges_are_amplitudes_too(self) -> None:
        """L'ATR ne peut pas être positif si ses termes ne le sont pas."""
        for index in selected_cases():
            for value in ae.true_ranges(case_series(index)):
                self.assertGreaterEqual(value, 0.0, f"cas {index} : true range négatif")

    def test_an_inverted_candle_does_not_make_the_atr_negative(self) -> None:
        """Une ligne mal formée d'une source de marché (`high` < `low`).

        C'est ce cas qui a rendu la première chandelle absolue : sa valeur partait
        négative dans la RMA, qui ne la résorbe qu'après des dizaines de
        chandelles — le temps de faire rejeter tous les croisements, sans un mot.
        """
        series = flat_candles([10.0, 10.0, 10.0, 10.0]) + flat_candles([10.0] * 20)
        series[0] = {"open": 10.0, "high": 8.0, "low": 12.0, "close": 10.0}
        for period in (2, 5, 14):
            value = ae.atr(series, period)
            self.assertIsNotNone(value)
            self.assertGreaterEqual(value, 0.0, f"période {period}")

    def test_the_atr_stays_between_zero_and_the_largest_true_range(self) -> None:
        """L'ATR est une moyenne à poids positifs de somme 1 des true ranges.

        Il ne peut donc ni passer sous zéro, ni dépasser le plus grand d'entre
        eux — la borne haute attrape un coefficient faux, que la borne basse
        laisserait passer.
        """
        for index in selected_cases():
            series = case_series(index)
            ranges = ae.true_ranges(series)
            for period in PERIODS:
                value = ae.atr(series, period)
                if value is None:
                    continue
                self.assertLessEqual(value, max(ranges) + 1e-9, f"cas {index} (période {period})")

    def test_a_flat_market_keeps_a_zero_atr(self) -> None:
        """Zéro est la borne : il doit rester **zéro**, et pas devenir négatif."""
        self.assertEqual(ae.atr(flat_candles([7.0] * 30), 14), 0.0)


class EmaStaysInsideTheCloseEnvelopeTest(unittest.TestCase):
    """L'EMA est une combinaison convexe des clôtures : elle ne peut pas en sortir."""

    def test_over_every_enumerated_series_and_span(self) -> None:
        for index in selected_cases():
            closes = [candle["close"] for candle in case_series(index)]
            low, high = min(closes), max(closes)
            margin = max(abs(low), abs(high), 1.0) * 1e-12
            for span in SPANS:
                for value in ae.ema(closes, span):
                    self.assertGreaterEqual(
                        value,
                        low - margin,
                        f"cas {index} (span {span}) : EMA sous l'enveloppe — rejoue "
                        f"ALERT_PROPERTY_CASE={index}",
                    )
                    self.assertLessEqual(value, high + margin, f"cas {index} (span {span})")

    def test_it_holds_at_every_step_and_not_only_at_the_end(self) -> None:
        """L'enveloppe est celle des clôtures **déjà vues** : la récursion est causale.

        Une EMA qui sortirait de l'enveloppe au milieu de la série produirait un
        croisement que le marché n'a jamais eu.
        """
        for index in selected_cases():
            closes = [candle["close"] for candle in case_series(index)]
            for span in (5, 20, 50):
                if len(closes) < span:
                    continue
                values = ae.ema(closes, span)
                for position, value in enumerate(values):
                    window = closes[: span + position]
                    self.assertGreaterEqual(value, min(window) - 1e-9, f"cas {index} span {span}")
                    self.assertLessEqual(value, max(window) + 1e-9, f"cas {index} span {span}")

    def test_it_holds_when_the_prices_cross_zero(self) -> None:
        """La convexité ne dépend pas du signe : un actif qui passe sous zéro reste borné."""
        closes = [10.0, 5.0, 0.0, -5.0, -10.0, -20.0, -3.0, 8.0]
        values = ae.ema(closes, 3)
        self.assertEqual(len(values), len(closes) - 2)
        for value in values:
            self.assertGreaterEqual(value, min(closes) - 1e-9)
            self.assertLessEqual(value, max(closes) + 1e-9)

    def test_only_the_span_and_the_length_decide_to_render_nothing(self) -> None:
        """`span` invalide ou historique trop court : une liste vide, jamais une valeur."""
        self.assertEqual(ae.ema([1.0, 2.0, 3.0], 0), [])
        self.assertEqual(ae.ema([1.0, 2.0], 3), [])
        self.assertEqual(ae.ema([], 1), [])


class CrossGeometryTest(unittest.TestCase):
    """Pourquoi le signe de l'ATR compte : c'est lui qui met le stop du bon côté.

    Une marche aléatoire ne croise presque jamais **sur la dernière chandelle**,
    et `detect_cross` ne regarde que celle-là : les séries sont donc construites
    pour croiser à coup sûr (tendance, palier, retournement final). Les formes
    retenues sont celles qui croisent réellement, vérifiées en les énumérant —
    sans quoi le test ne porterait que sur des cas que le hasard n'a pas
    produits, c'est-à-dire sur rien.
    """

    #: (pente relative, longueur du palier, saut final relatif) — palier et saut
    #: sont ce qui permet à l'EMA rapide de repasser devant la lente.
    SHAPES = ((0.002, 60, 0.08), (0.004, 140, 0.02), (0.008, 140, 0.04))
    EMA_PAIRS = ((20, 50), (5, 20))
    #: Un bruit minuscule : le croisement doit survivre à des clôtures qui ne
    #: tombent pas juste, comme celles d'un vrai flux.
    NOISES = (0.0, 1e-4)

    @staticmethod
    def crossing_series(
        *, upward, trend, plateau, jump, moves=60, price=100.0, noise=0.0, seed=1
    ) -> list:
        """Une tendance, un palier, puis le retournement **sur la dernière chandelle**."""
        rng = random.Random(f"cross:{seed}")
        closes = [price]
        for _ in range(moves):
            closes.append(closes[-1] + (-trend if upward else trend) * price)
        closes.extend([closes[-1]] * plateau)
        closes.append(closes[-1] + (jump if upward else -jump) * price)
        if noise:
            closes = [close * (1.0 + rng.uniform(-noise, noise)) for close in closes]
        return flat_candles(closes)

    def test_a_signal_puts_the_stop_and_the_target_on_the_right_sides(self) -> None:
        """Pour toute forme, tout multiplicateur et toute période : SL sous le prix.

        C'est l'ATR positif qui garantit cette géométrie ; un ATR négatif
        l'inverserait sans qu'aucune erreur ne soit levée. La propriété est
        vérifiée sur **chaque** croisement de l'énumération, et l'énumération doit
        en produire : une liste vide passerait tous les `for` sans rien prouver.
        """
        checked = 0
        for trend, plateau, jump in self.SHAPES:
            for upward in (True, False):
                for noise in self.NOISES:
                    series = self.crossing_series(
                        upward=upward, trend=trend, plateau=plateau, jump=jump, noise=noise
                    )
                    for ema_fast, ema_slow in self.EMA_PAIRS:
                        for atr_period in (5, 14, 21):
                            for stop_mult in (0.5, 1.5, 3.0):
                                for target_mult in (1.0, 3.0, 6.0):
                                    checked += self._assert_geometry(
                                        series,
                                        ema_fast,
                                        ema_slow,
                                        atr_period,
                                        stop_mult,
                                        target_mult,
                                    )
        self.assertGreater(
            checked, 20, "trop peu de croisements : le test ne prouverait presque rien"
        )

    def _assert_geometry(
        self, series, ema_fast, ema_slow, atr_period, stop_mult, target_mult
    ) -> int:
        """Un croisement, c'est deux côtés : rend 1 s'il y en a eu un, 0 sinon."""
        alert = ae.detect_cross(
            series,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            atr_period=atr_period,
            sl_atr_mult=stop_mult,
            tp_atr_mult=target_mult,
        )
        if alert is None:
            return 0
        self.assertGreater(alert["atr"], 0.0, "un signal ne peut pas porter un ATR nul")
        price = alert["price"]
        if alert["action"] == "buy":
            self.assertLess(alert["stop_loss"], price)
            self.assertGreater(alert["take_profit"], price)
        else:
            self.assertGreater(alert["stop_loss"], price)
            self.assertLess(alert["take_profit"], price)
        return 1

    def test_the_geometry_survives_an_inverted_candle(self) -> None:
        """Même avec une ligne mal formée, le stop reste du bon côté du prix.

        C'est le cas que la première chandelle absolue a rendu inoffensif : un
        true range négatif y faisait plonger l'ATR sous zéro, et `detect_cross`
        rejetait alors le croisement (`volatility <= 0`) — le signal disparaissait
        au lieu d'être faux, ce qui est plus difficile à voir.
        """
        checked = 0
        for trend, plateau, jump in self.SHAPES:
            series = self.crossing_series(
                upward=True, trend=trend, plateau=plateau, jump=jump
            )
            first = series[0]
            series[0] = {
                "open": first["close"],
                "high": first["low"],
                "low": first["high"],
                "close": first["close"],
            }
            checked += self._assert_geometry(series, 20, 50, 14, 1.5, 3.0)
        self.assertGreater(checked, 0, "la série mal formée ne croise plus : à revoir")


if __name__ == "__main__":
    unittest.main()
