"""Tests de l'historique des recherches (`core/search_history.py`).

Ce module existe pour une raison précise : `/search_more` doit afficher la suite
d'un lot **déjà interrogé**, sans nouvel appel réseau et sans reclasser les
passages. Les tests couvrent donc trois propriétés et pas seulement le cas
nominal :

* la pagination ne perd ni ne répète un passage (c'est tout l'intérêt du lot) ;
* la taille de page suit le `-n` de la recherche d'origine ;
* les deux échecs (`no_history`, `exhausted`) sont distincts, parce qu'ils n'ont
  pas le même remède.
"""

from __future__ import annotations

import unittest

from core import search_history


def _hits(count: int, start: int = 0):
    return [
        {"content": f"passage {index}", "similarity": 0.9 - index / 100}
        for index in range(start, start + count)
    ]


def _options(top_k: int = 5, **overrides):
    options = {
        "ok": True,
        "reason": None,
        "query": "cassure des 100k",
        "asset": None,
        "source": None,
        "regime": None,
        "top_k": top_k,
    }
    options.update(overrides)
    return options


class SearchHistoryTest(unittest.TestCase):
    def setUp(self):
        search_history.reset()
        self.addCleanup(search_history.reset)

    # -- première page ----------------------------------------------------- #

    def test_first_page_is_the_head_of_the_pool(self):
        page = search_history.start("u1", _options(top_k=2), _hits(5))
        self.assertTrue(page["ok"])
        self.assertEqual([hit["content"] for hit in page["hits"]], ["passage 0", "passage 1"])
        self.assertEqual(page["start"], 1)
        self.assertEqual(page["total"], 5)
        self.assertEqual(page["query"], "cassure des 100k")

    def test_whole_pool_fits_in_one_page(self):
        page = search_history.start("u1", _options(top_k=10), _hits(3))
        self.assertEqual(len(page["hits"]), 3)
        self.assertEqual(search_history.latest("u1").remaining, 0)

    def test_empty_pool_is_recorded_and_paginates_to_nothing(self):
        page = search_history.start("u1", _options(), [])
        self.assertEqual(page["hits"], [])
        self.assertEqual(page["total"], 0)
        self.assertEqual(search_history.next_page("u1")["reason"], "exhausted")

    def test_hits_are_kept_verbatim(self):
        """Le contenu complet est conservé : sinon il faudrait redemander au réseau."""
        pool = _hits(4)
        pool[2]["content"] = "texte intégral " + "x" * 500
        search_history.start("u1", _options(top_k=1), pool)
        page = search_history.next_page("u1")
        page = search_history.next_page("u1")
        self.assertEqual(page["hits"][0]["content"], pool[2]["content"])

    # -- pages suivantes --------------------------------------------------- #

    def test_next_page_continues_the_ranking(self):
        search_history.start("u1", _options(top_k=2), _hits(5))
        page = search_history.next_page("u1")
        self.assertTrue(page["ok"])
        self.assertEqual([hit["content"] for hit in page["hits"]], ["passage 2", "passage 3"])
        self.assertEqual(page["start"], 3, "la numérotation doit continuer, pas repartir à 1")
        self.assertEqual(page["total"], 5)

    def test_walking_the_pool_sees_every_passage_exactly_once(self):
        first = search_history.start("u1", _options(top_k=3), _hits(7))
        seen = [hit["content"] for hit in first["hits"]]
        while True:
            page = search_history.next_page("u1")
            if not page["ok"]:
                self.assertEqual(page["reason"], "exhausted")
                break
            seen.extend(hit["content"] for hit in page["hits"])
        self.assertEqual(seen, [f"passage {index}" for index in range(7)])

    def test_page_size_follows_the_original_search(self):
        """`-n 2` doit valoir pour les pages suivantes, pas seulement la première."""
        search_history.start("u1", _options(top_k=2), _hits(6))
        sizes = [len(search_history.next_page("u1")["hits"]) for _ in range(2)]
        self.assertEqual(sizes, [2, 2])

    def test_exhausted_reports_what_was_shown(self):
        search_history.start("u1", _options(top_k=5), _hits(5))
        result = search_history.next_page("u1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "exhausted")
        self.assertEqual(result["shown"], 5)
        self.assertEqual(result["total"], 5)
        self.assertEqual(result["query"], "cassure des 100k")

    def test_no_history_is_its_own_failure(self):
        result = search_history.next_page("inconnu")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no_history")

    def test_history_is_per_user(self):
        search_history.start("u1", _options(top_k=1), _hits(4))
        self.assertEqual(search_history.next_page("u2")["reason"], "no_history")

    # -- cycle de vie ------------------------------------------------------ #

    def test_only_the_latest_search_is_kept(self):
        """Une seconde recherche remplace la première : `/search_more` la continue."""
        search_history.start("u1", _options(top_k=2), _hits(6))
        search_history.start("u1", _options(top_k=2, query="autre chose"), _hits(4))
        page = search_history.next_page("u1")
        self.assertEqual(page["query"], "autre chose")
        self.assertEqual(page["start"], 3)
        self.assertEqual(len(search_history.latest("u1").pool), 4)

    def test_forget_drops_a_single_user(self):
        search_history.start("u1", _options(), _hits(2))
        search_history.forget("u1")
        self.assertIsNone(search_history.latest("u1"))

    def test_oldest_user_is_evicted_when_the_bound_is_reached(self):
        """Le bot tourne en continu : la borne d'utilisateurs doit être réelle."""
        for index in range(search_history.MAX_USERS + 3):
            search_history.start(f"u{index}", _options(top_k=1), _hits(3))
        self.assertIsNone(search_history.latest("u0"))
        self.assertIsNotNone(search_history.latest(f"u{search_history.MAX_USERS + 2}"))
        # L'utilisateur actif reste : c'est un LRU, pas une file FIFO arbitraire.
        search_history.start("u1", _options(top_k=1), _hits(3))
        self.assertIsNotNone(search_history.latest("u1"))

    def test_reading_a_session_does_not_advance_it(self):
        search_history.start("u1", _options(top_k=1), _hits(3))
        search_history.latest("u1")
        self.assertEqual(search_history.next_page("u1")["start"], 2)

    def test_a_bogus_page_size_still_yields_a_page(self):
        """Appel direct (test, script) : jamais de page vide à cause d'un `-n` absurde."""
        search_history.start("u1", _options(top_k=0), _hits(2))
        self.assertEqual(len(search_history.next_page("u1")["hits"]), 1)


if __name__ == "__main__":
    unittest.main()
