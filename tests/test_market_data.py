"""La chaîne des sources de prix : ce qui est **nommé**, et ce qui reste muet.

`utils/market_data.py` essaie plusieurs fournisseurs à la suite (Finnhub, Yahoo,
Binance, CoinGecko, Stooq). Un échec *silencieux* y est le mode de panne le plus
coûteux : « Yahoo n'a rien rendu » (symbole inconnu, marché fermé) et « Yahoo est
injoignable » (réseau coupé, `httpx` absent, réponse illisible) produisaient le
même `None` — l'appelant passait alors à la source suivante comme si Yahoo avait
simplement répondu vide. L'absence de `httpx`, surtout, ne se voyait nulle part :
toutes les sources HTTP échouaient dans le même silence, et l'actif finissait
« sans prix » sans qu'aucune ligne ne dise pourquoi.

Ce qui est éprouvé tient donc en deux volets :

* **une panne est nommée** — dépendance absente, hôte injoignable, statut non-200 :
  le motif sort sur la console, jamais avalé ;
* **un succès reste silencieux** — une réponse exploitable est rendue sans bruit,
  et une réponse *vide mais aboutie* n'invente pas une panne.
"""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from utils import market_data


class _FakeClient:
    """Un client `httpx` minimal : la seule surface que `_yahoo_chart` utilise."""

    def __init__(self, *, status_code=200, payload=None, error=None):
        self._status_code = status_code
        self._payload = payload
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, url):
        if self._error is not None:
            raise self._error
        return SimpleNamespace(status_code=self._status_code, json=lambda: self._payload)


def _httpx_stub(**client_kwargs):
    """Un faux module `httpx` dont chaque `Client(...)` rend le même client."""
    return SimpleNamespace(Client=lambda *args, **kwargs: _FakeClient(**client_kwargs))


class YahooChartFailureNamingTest(unittest.TestCase):
    """`_yahoo_chart` ne rend `None` qu'en disant **pourquoi**."""

    def _run(self, httpx_module):
        out = io.StringIO()
        with mock.patch.object(market_data, "httpx", httpx_module):
            with redirect_stdout(out):
                result = market_data._yahoo_chart("BTC-USD")
        return result, out.getvalue()

    def test_a_missing_dependency_is_named(self):
        # `httpx = None` (import raté) : `httpx.Client` lève un AttributeError qui
        # était avalé — toutes les sources HTTP échouaient alors en silence.
        result, out = self._run(None)
        self.assertIsNone(result)
        self.assertIn("AttributeError", out)

    def test_an_unreachable_host_is_named(self):
        result, out = self._run(_httpx_stub(error=RuntimeError("coupure réseau")))
        self.assertIsNone(result)
        self.assertIn("RuntimeError", out)

    def test_a_non_200_status_is_named(self):
        result, out = self._run(_httpx_stub(status_code=429, payload={"chart": {"result": []}}))
        self.assertIsNone(result)
        self.assertIn("HTTP 429", out)

    def test_a_usable_answer_is_returned_without_a_word(self):
        payload = {"chart": {"result": [{"meta": {"regularMarketPrice": 42}}]}}
        result, out = self._run(_httpx_stub(payload=payload))
        self.assertEqual(result, {"meta": {"regularMarketPrice": 42}})
        self.assertEqual(out, "")

    def test_an_empty_but_successful_answer_is_not_reported_as_a_failure(self):
        result, out = self._run(_httpx_stub(payload={"chart": {"result": []}}))
        self.assertIsNone(result)
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
