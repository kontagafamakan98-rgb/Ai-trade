"""Tests du filtre de risque « news » — et surtout de sa date illisible.

Le défaut corrigé ici est le seul du dépôt où un `except` **fabriquait** une
décision : `parse_event_dt` retombait sur « maintenant », ce qui faisait entrer
l'événement dans toutes les fenêtres (`time_diff ≈ 0`) et bloquait le symbole au
motif d'un « événement imminent » que rien n'étayait. Une date `NULL` en base
suffisait donc à interdire le trading d'un actif, avec un motif faux.

Ce que ces tests fixent :

* `parse_event_dt` rend `None` — jamais l'instant présent — quand la date est
  illisible ;
* un événement non datable est **nommé** dans le verdict (`undated_events`) et
  traité par précaution, avec un motif qui dit la vraie raison ;
* un événement *datable* dans sa fenêtre garde la priorité sur une précaution :
  quand on sait, on ne devine pas.
"""

import unittest
from datetime import datetime, timedelta, timezone

from execution.news_risk_guard import check_news_risk, parse_event_dt

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
SYMBOL = "BTC-USD"


def _event(impact="High", currency="USD", title="Non-Farm Payrolls", event_date=None):
    return {
        "title": title,
        "currency": currency,
        "impact": impact,
        "event_date": event_date,
    }


class ParseEventDateTest(unittest.TestCase):
    """`parse_event_dt` situe un événement, ou avoue qu'il ne peut pas."""

    def test_nothing_to_parse_is_not_now(self):
        for raw in (None, "", "   ", "pas une date", "None", "null", 12345, {}):
            with self.subTest(raw=raw):
                self.assertIsNone(
                    parse_event_dt(raw),
                    "une date illisible ne doit jamais devenir l'instant présent",
                )

    def test_an_aware_iso_string_is_converted_to_utc(self):
        parsed = parse_event_dt("2026-09-26T14:00:00+02:00")
        self.assertEqual(parsed, datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc))

    def test_a_zulu_suffix_is_understood(self):
        parsed = parse_event_dt("2026-09-26T12:00:00Z")
        self.assertEqual(parsed, NOW)

    def test_datetime_objects_pass_through(self):
        aware = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_event_dt(aware), aware)
        naive = datetime(2026, 9, 26, 12, 0)
        self.assertEqual(parse_event_dt(naive).tzinfo, timezone.utc)


class UndatedEventTest(unittest.TestCase):
    """Un événement qu'on ne sait pas situer est un fait du rapport, pas un silence."""

    def test_a_high_event_with_no_date_blocks_by_precaution_naming_the_reason(self):
        risk = check_news_risk(SYMBOL, [_event()], now_dt=NOW)
        self.assertFalse(risk["allowed"])
        self.assertEqual(risk["risk_level"], "BLOCK")
        self.assertIn("illisible", risk["reason"])
        self.assertIn("précaution", risk["reason"])
        self.assertNotIn(
            "imminent",
            risk["reason"],
            "le motif ne doit pas affirmer une fenêtre qu'on n'a pas pu vérifier",
        )
        self.assertEqual(len(risk["undated_events"]), 1)

    def test_a_null_date_is_not_read_as_now(self):
        """Le cas de base : une colonne `event_date` vide en production."""
        risk = check_news_risk(SYMBOL, [_event(event_date=None)], now_dt=NOW)
        self.assertFalse(risk["allowed"])
        self.assertNotIn("dans 0 min", risk["reason"])
        self.assertNotIn("il y a 0 min", risk["reason"])
        self.assertEqual(risk["undated_events"][0]["title"], "Non-Farm Payrolls")

    def test_a_garbage_date_string_is_not_read_as_now(self):
        risk = check_news_risk(SYMBOL, [_event(event_date="n/a")], now_dt=NOW)
        self.assertFalse(risk["allowed"])
        self.assertEqual(risk["risk_level"], "BLOCK")
        self.assertNotIn("dans 0 min", risk["reason"])

    def test_a_medium_undated_event_degrades_without_blocking(self):
        risk = check_news_risk(SYMBOL, [_event(impact="Medium", title="Retail Sales")], now_dt=NOW)
        self.assertTrue(risk["allowed"])
        self.assertEqual(risk["risk_level"], "DEGRADE")
        self.assertIn("illisible", risk["reason"])
        self.assertEqual(len(risk["undated_events"]), 1)

    def test_a_low_undated_event_changes_nothing(self):
        """Ce qui ne bloquait pas quand c'était datable ne bloque pas parce que c'est flou."""
        risk = check_news_risk(SYMBOL, [_event(impact="Low")], now_dt=NOW)
        self.assertTrue(risk["allowed"])
        self.assertEqual(risk["risk_level"], "ALLOWED")

    def test_an_irrelevant_currency_is_still_filtered_before_parsing(self):
        risk = check_news_risk(
            SYMBOL, [_event(currency="EUR", event_date="pas une date")], now_dt=NOW
        )
        self.assertTrue(risk["allowed"])
        self.assertEqual(risk["undated_events"], [])

    def test_no_event_at_all_is_plainly_allowed(self):
        risk = check_news_risk(SYMBOL, [], now_dt=NOW)
        self.assertTrue(risk["allowed"])
        self.assertEqual(risk["undated_events"], [])

    def test_a_known_window_wins_over_an_undated_precaution(self):
        """Quand on sait, on ne devine pas : le motif parle de l'événement daté."""
        dated = _event(
            title="FOMC Statement",
            event_date=(NOW + timedelta(minutes=5)).isoformat(),
        )
        risk = check_news_risk(SYMBOL, [_event(title="Vieux calendrier"), dated], now_dt=NOW)
        self.assertFalse(risk["allowed"])
        self.assertIn("FOMC Statement", risk["reason"])
        self.assertNotIn("précaution", risk["reason"])
        self.assertEqual(len(risk["undated_events"]), 1, "l'autre reste nommé")

    def test_a_dated_event_outside_its_window_leaves_room_for_the_precaution(self):
        far = _event(
            impact="High",
            title="FOMC Statement",
            event_date=(NOW + timedelta(hours=5)).isoformat(),
        )
        shaky = _event(impact="Medium", title="Retail Sales", event_date="illisible")
        risk = check_news_risk(SYMBOL, [far, shaky], now_dt=NOW)
        self.assertTrue(risk["allowed"])
        self.assertEqual(risk["risk_level"], "DEGRADE")
        self.assertIn("Retail Sales", risk["reason"])


class KnownWindowTest(unittest.TestCase):
    """Le comportement d'origine, inchangé, sur les dates qui se lisent."""

    def test_high_impact_block_degrade_and_allow(self):
        near = check_news_risk(
            SYMBOL,
            [_event(event_date=(NOW + timedelta(minutes=10)).isoformat())],
            now_dt=NOW,
        )
        self.assertFalse(near["allowed"])
        self.assertIn("BLOQUÉ", near["reason"])

        medium = check_news_risk(
            SYMBOL,
            [_event(impact="Medium", event_date=(NOW + timedelta(minutes=10)).isoformat())],
            now_dt=NOW,
        )
        self.assertEqual(medium["risk_level"], "DEGRADE")

        old = check_news_risk(
            SYMBOL,
            [_event(event_date=(NOW - timedelta(hours=2)).isoformat())],
            now_dt=NOW,
        )
        self.assertTrue(old["allowed"])
        self.assertEqual(old["risk_level"], "ALLOWED")
        self.assertEqual(old["undated_events"], [])


if __name__ == "__main__":
    unittest.main()
