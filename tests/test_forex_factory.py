import unittest
from datetime import datetime, timezone, timedelta
from scrapers.forex_factory import parse_numeric_value, normalize_impact, ForexFactoryClient
from core.macro_engine import (
    evaluate_event_impact,
    calculate_currency_macro_score,
    calculate_symbol_macro_bias,
    get_currencies_for_symbol
)
from execution.news_risk_guard import check_news_risk
from ai.decision_engine import EmotionlessDecisionEngine


class TestForexFactoryIntegration(unittest.TestCase):

    def test_parse_numeric_value(self):
        """Test string numerical value parsing with various formats and suffixes."""
        self.assertEqual(parse_numeric_value("0.3%"), 0.3)
        self.assertEqual(parse_numeric_value("-1.5%"), -1.5)
        self.assertEqual(parse_numeric_value("220K"), 220000.0)
        self.assertEqual(parse_numeric_value("1.5M"), 1500000.0)
        self.assertEqual(parse_numeric_value("$12.5B"), 12500000000.0)
        self.assertEqual(parse_numeric_value("1,250"), 1250.0)
        self.assertIsNone(parse_numeric_value("N/A"))
        self.assertIsNone(parse_numeric_value("--"))
        self.assertIsNone(parse_numeric_value(None))

    def test_normalize_impact(self):
        """Test normalization of Forex Factory impact labels."""
        self.assertEqual(normalize_impact("High Impact"), "High")
        self.assertEqual(normalize_impact("Red"), "High")
        self.assertEqual(normalize_impact("Medium"), "Medium")
        self.assertEqual(normalize_impact("Yellow"), "Low")
        self.assertEqual(normalize_impact("Non-Economic"), "Non-Economic")

    def test_actual_vs_forecast_comparison(self):
        """Test economic event impact evaluation (Standard vs Inverse indicators)."""
        # CPI Event: Actual > Forecast -> Bullish USD
        cpi_event = {
            "title": "Core CPI m/m",
            "currency": "USD",
            "impact": "High",
            "forecast_num": 0.3,
            "actual_num": 0.5,
            "previous_num": 0.3
        }
        score, reason = evaluate_event_impact(cpi_event)
        self.assertGreater(score, 0.0)
        self.assertIn("Bullish", reason)

        # Unemployment Rate Event: Actual > Forecast -> Bearish USD (Inverse indicator)
        unemp_event = {
            "title": "Unemployment Rate",
            "currency": "USD",
            "impact": "High",
            "forecast_num": 3.8,
            "actual_num": 4.1,
            "previous_num": 3.8
        }
        unemp_score, unemp_reason = evaluate_event_impact(unemp_event)
        self.assertLess(unemp_score, 0.0)
        self.assertIn("Bearish", unemp_reason)

    def test_macro_bias_calculation(self):
        """Test symbol macro bias score calculation."""
        events = [
            {
                "title": "Non-Farm Employment Change",
                "currency": "USD",
                "impact": "High",
                "forecast_num": 180000.0,
                "actual_num": 250000.0,
                "previous_num": 175000.0
            },
            {
                "title": "Core CPI m/m",
                "currency": "USD",
                "impact": "High",
                "forecast_num": 0.3,
                "actual_num": 0.5,
                "previous_num": 0.3
            }
        ]

        score, bias_label, reasons = calculate_symbol_macro_bias("EURUSD", events)
        self.assertIn(bias_label, ["BEARISH", "BULLISH", "NEUTRAL"])
        self.assertGreater(len(reasons), 0)

    def test_news_risk_guard_blocking_and_degrading(self):
        """Test news risk guard window logic (High impact block, Medium impact degrade)."""
        now_dt = datetime.now(timezone.utc)

        # 1. High impact event in 10 minutes -> BLOCK
        high_impact_event = {
            "title": "Non-Farm Payrolls",
            "currency": "USD",
            "impact": "High",
            "event_date": (now_dt + timedelta(minutes=10)).isoformat()
        }
        risk = check_news_risk("BTC-USD", [high_impact_event], now_dt=now_dt)
        self.assertFalse(risk["allowed"])
        self.assertEqual(risk["risk_level"], "BLOCK")
        self.assertIn("BLOQUÉ", risk["reason"])

        # 2. Medium impact event in 10 minutes -> DEGRADE
        med_impact_event = {
            "title": "Retail Sales",
            "currency": "USD",
            "impact": "Medium",
            "event_date": (now_dt + timedelta(minutes=10)).isoformat()
        }
        risk_med = check_news_risk("BTC-USD", [med_impact_event], now_dt=now_dt)
        self.assertTrue(risk_med["allowed"])
        self.assertEqual(risk_med["risk_level"], "DEGRADE")

        # 3. High impact event 2 hours ago -> ALLOWED
        old_event = {
            "title": "GDP q/q",
            "currency": "USD",
            "impact": "High",
            "event_date": (now_dt - timedelta(hours=2)).isoformat()
        }
        risk_old = check_news_risk("BTC-USD", [old_event], now_dt=now_dt)
        self.assertTrue(risk_old["allowed"])
        self.assertEqual(risk_old["risk_level"], "ALLOWED")


if __name__ == "__main__":
    unittest.main()
