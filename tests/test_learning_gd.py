"""Tests de l'apprentissage des poids par descente de gradient (opt-in).

Note sur l'ordre d'exécution : `config.py` et `notifications/notify.py` figent leurs
valeurs à l'import et `test_api_auth_integration.py` injecte sa fausse config en
s'imposant comme **premier** module à importer `config`. Ce fichier ne doit donc
pas précéder `test_api_auth_integration.py` dans l'ordre alphabétique de
découverte (`test_learning_*` > `test_api_*`), sinon le token Telegram factice de
l'intégration n'est plus honoré et ses 7 tests se sautent silencieusement.

Trois propriétés sont verrouillées ici :

1. les sous-notes réelles (TA / sentiment / macro) sont persistées avec le
   post-mortem au lieu de la constante 0.5 ;
2. l'apprentissage par descente de gradient reste **inactif par défaut** et ne
   remplace les poids heuristiques que lorsque `ADAPTIVE_GD_ENABLED` est activé
   *et* qu'il y a assez de trades réglés — sinon le comportement nominal est
   strictement inchangé ;
3. un signal réglé **une** fois : le trade en cours est exclu de son propre
   entraînement (`SelfTrainingExclusionTest`) et un rejeu du même signal n'écrit ni
   seconde ligne ni second compteur (`SettlementIdempotenceTest`).
"""

import os
import pathlib
import unittest
from unittest import mock

from core import adaptive_learning as al
from core.config_runtime import reset_env_config
from tests import supabase_double


# --------------------------------------------------------------------------- #
# L'historique réglé est semé sur la doublure **partagée** du dossier
# (`tests/supabase_double.py`) : les écritures y persistent et les filtres y sont
# appliqués comme PostgREST les applique. Un historique relu est donc vraiment
# celui qu'on a semé, et il doit porter la colonne sur laquelle la lecture filtre
# — ici `asset`, sans quoi `_fetch_settled_post_mortems` rendrait une base vide et
# la descente de gradient retomberait en silence sur l'heuristique.
# --------------------------------------------------------------------------- #


def _upsert_payload(client, table):
    """La charge écrite par `upsert` — le journal y joint la cible du conflit."""
    payload, _on_conflict = client.store(table).upserted[0]
    return payload


def _history(
    count, outcome_pattern="won", ta=0.8, sentiment=0.2, macro=0.2, asset="EURUSD"
):
    """Historique réglé d'un actif, dont les sous-notes favorisent nettement TA."""
    rows = []
    for i in range(count):
        outcome = outcome_pattern
        if outcome_pattern == "mixed":
            outcome = "won" if (i % 4) in (0, 1, 2) else "lost"
        rows.append(
            {
                "asset": asset,
                "outcome": outcome,
                "ta_score": ta,
                "sentiment_score": sentiment,
                "macro_score": macro,
            }
        )
    return rows


class AdaptiveLearningGdTestBase(unittest.TestCase):
    def setUp(self):
        self._env_backup = {
            key: os.environ.get(key)
            for key in ("ADAPTIVE_GD_ENABLED", "ADAPTIVE_GD_MIN_SAMPLES")
        }
        for key in self._env_backup:
            os.environ.pop(key, None)
        reset_env_config()
        #: Le client doublé, posé sur `al.supabase` pour tout le test : c'est le
        #: règlement qui décide **où** il écrit, pas le test.
        self.client = supabase_double.use_supabase(
            self, supabase_double.SupabaseDouble(), al
        )

    def tearDown(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_env_config()

    def _enable(self, min_samples):
        os.environ["ADAPTIVE_GD_ENABLED"] = "true"
        os.environ["ADAPTIVE_GD_MIN_SAMPLES"] = str(min_samples)
        reset_env_config()

    def _run(self, signal, outcome, history, exit_price=None):
        """Un règlement, avec l'historique réglé de l'actif semé en base."""
        self.client.store("trade_post_mortems").rows = [dict(row) for row in history]
        with mock.patch.object(al, "upsert_note", lambda **kwargs: None):
            result = al.record_trade_settlement_and_learn(signal, outcome, exit_price)
        return self.client, result


# --------------------------------------------------------------------------- #
# 1. Persistance des vraies sous-notes
# --------------------------------------------------------------------------- #


class SubscorePersistenceTest(AdaptiveLearningGdTestBase):
    def test_real_subscores_are_persisted(self):
        signal = {
            "id": "sig_42",
            "asset": "EURUSD",
            "direction": "BUY",
            "confidence": 0.71,
            "ta_score": 0.72,
            "sentiment_score": 0.31,
            "macro_score": 0.64,
        }
        fake, result = self._run(signal, "lost", [])
        row = fake.store("trade_post_mortems").inserted[0]
        self.assertAlmostEqual(row["ta_score"], 0.72, places=4)
        self.assertAlmostEqual(row["sentiment_score"], 0.31, places=4)
        self.assertAlmostEqual(row["macro_score"], 0.64, places=4)
        self.assertEqual(result["subscores"], {"ta": 0.72, "sentiment": 0.31, "macro": 0.64})

    def test_missing_subscores_fall_back_to_neutral(self):
        """Un signal historique sans sous-notes reste insérable (valeur neutre)."""
        signal = {"id": "sig_1", "asset": "EURUSD", "direction": "BUY", "confidence": 0.6}
        fake, _ = self._run(signal, "won", [])
        row = fake.store("trade_post_mortems").inserted[0]
        self.assertEqual(row["ta_score"], 0.5)
        self.assertEqual(row["sentiment_score"], 0.5)
        self.assertEqual(row["macro_score"], 0.5)

    def test_non_numeric_subscore_falls_back_to_neutral(self):
        signal = {
            "id": "sig_2",
            "asset": "EURUSD",
            "direction": "BUY",
            "ta_score": "not-a-number",
            "sentiment_score": None,
            "macro_score": 1.7,  # borné à 1.0
        }
        fake, _ = self._run(signal, "won", [])
        row = fake.store("trade_post_mortems").inserted[0]
        self.assertEqual(row["ta_score"], 0.5)
        self.assertEqual(row["sentiment_score"], 0.5)
        self.assertEqual(row["macro_score"], 1.0)


# --------------------------------------------------------------------------- #
# 2. Le flag est bien OFF par défaut
# --------------------------------------------------------------------------- #


class FlagOffTest(AdaptiveLearningGdTestBase):
    def test_flag_disabled_by_default(self):
        from core.config_runtime import get_env_config

        self.assertFalse(get_env_config().adaptive_gd_enabled)

    def test_heuristic_weights_are_kept_when_flag_is_off(self):
        """Même avec un historique massif, rien ne change si le flag est inactif."""
        signal = {"id": "sig_3", "asset": "EURUSD", "direction": "BUY", "confidence": 0.7}
        # Un trade perdu avec un correctif "boost_macro_weight" déplace les poids.
        signal["geo_summary"] = "Macro BEARISH"
        fake, result = self._run(signal, "lost", _history(80, "mixed"))

        self.assertEqual(result["weight_update"], "heuristic")
        self.assertIsNone(result["gradient_descent"])
        upserted = _upsert_payload(fake, "adaptive_model_weights")
        # Heuristique d'origine : macro +0.05, TA -0.05 par rapport aux défauts.
        self.assertAlmostEqual(upserted["macro_weight"], 0.35, places=3)
        self.assertAlmostEqual(upserted["ta_weight"], 0.35, places=3)


# --------------------------------------------------------------------------- #
# 3. Le flag activé bascule réellement sur la descente de gradient
# --------------------------------------------------------------------------- #


class FlagOnTest(AdaptiveLearningGdTestBase):
    def test_learned_weights_replace_heuristic_when_enabled(self):
        self._enable(min_samples=20)
        signal = {"id": "sig_4", "asset": "EURUSD", "direction": "BUY", "confidence": 0.7}
        signal["geo_summary"] = "Macro BEARISH"  # heuristique : macro +0.05
        fake, result = self._run(signal, "lost", _history(60, "mixed"))

        self.assertEqual(result["weight_update"], "gradient_descent")
        self.assertIsNotNone(result["gradient_descent"])
        # 60 post-mortems semés, **sans** celui que ce règlement vient d'écrire :
        # l'historique est relu après l'insertion, mais le trade en cours en est
        # tenu hors (voir `_fetch_settled_post_mortems`).
        self.assertEqual(result["gradient_descent"]["samples"], 60)
        self.assertEqual(result["gradient_descent"]["excluded_signal_id"], "sig_4")

        weights = result["gradient_descent"]["weights"]
        self.assertAlmostEqual(sum(weights), 1.0, places=6)
        # Les sous-notes favorisent TA (0.8) face à sentiment/macro (0.2).
        self.assertEqual(max(range(3), key=lambda i: weights[i]), 0)

        upserted = _upsert_payload(fake, "adaptive_model_weights")
        self.assertAlmostEqual(upserted["ta_weight"], weights[0], places=3)
        self.assertAlmostEqual(upserted["macro_weight"], weights[2], places=3)
        # Le résultat doit être différent de l'heuristique seule.
        self.assertNotAlmostEqual(upserted["macro_weight"], 0.35, places=3)

    def test_insufficient_history_falls_back_to_heuristic(self):
        self._enable(min_samples=20)
        signal = {"id": "sig_5", "asset": "EURUSD", "direction": "BUY", "confidence": 0.7}
        fake, result = self._run(signal, "lost", _history(5, "mixed"))
        self.assertEqual(result["weight_update"], "heuristic")
        self.assertIsNone(result["gradient_descent"])
        self.assertTrue(fake.store("adaptive_model_weights").upserted)

    def test_gradient_descent_failure_never_breaks_settlement(self):
        """Une panne du calcul doit retomber sur l'heuristique, jamais lever."""
        self._enable(min_samples=20)
        signal = {"id": "sig_6", "asset": "EURUSD", "direction": "BUY", "confidence": 0.7}
        with mock.patch.object(al, "learn_weights_from_history", side_effect=RuntimeError("boom")):
            fake, result = self._run(signal, "lost", _history(60, "mixed"))
        self.assertEqual(result["weight_update"], "heuristic")
        self.assertTrue(fake.store("adaptive_model_weights").upserted)

    def test_settlement_still_records_post_mortem_when_gradient_descent_is_on(self):
        self._enable(min_samples=5)
        signal = {
            "id": "sig_7",
            "asset": "EURUSD",
            "direction": "BUY",
            "confidence": 0.7,
            "ta_score": 0.9,
            "sentiment_score": 0.1,
            "macro_score": 0.1,
        }
        fake, result = self._run(signal, "won", _history(30, "mixed"))
        self.assertEqual(result["status"], "learned")
        self.assertEqual(len(fake.store("trade_post_mortems").inserted), 1)
        self.assertEqual(result["post_mortem"]["corrective_action"], "maintain_weights")


# --------------------------------------------------------------------------- #
# 3bis. Le trade réglé n'entre pas dans son propre entraînement
# --------------------------------------------------------------------------- #


class SelfTrainingExclusionTest(AdaptiveLearningGdTestBase):
    """Un trade ne s'entraîne pas sur lui-même — et l'exclusion se fait par identité.

    Le règlement insère son post-mortem (étape 1) **puis** relit l'historique pour
    apprendre les poids (étape 2bis) : sans exclusion, la ligne qu'il vient
    d'écrire est le premier échantillon du fit, donc l'adaptation s'ajuste sur
    l'issue qu'elle sert à expliquer. Ces tests ne comparent pas à un chiffre
    attendu : ils comparent au fit **sans** ce trade-là, ce que la propriété dit.

    Si `signal_id` sortait de la projection de `_fetch_settled_post_mortems`, plus
    rien ne correspondrait et ces tests rougiraient : l'exclusion ne peut pas être
    vérifiée « en apparence ».
    """

    #: Le profil du trade réglé : l'opposé de l'historique semé, donc s'il entrait
    #: dans le fit, les poids bougeraient dans l'autre sens.
    MACRO_ROW = {"ta_score": 0.1, "sentiment_score": 0.1, "macro_score": 0.9}

    def _signal(self, signal_id, **extra):
        signal = {
            "id": signal_id,
            "asset": "EURUSD",
            "direction": "BUY",
            "confidence": 0.7,
            **extra,
        }
        return signal

    def test_the_settled_trade_is_absent_from_the_sample(self):
        """Le fit porte **exactement** sur les trades antérieurs."""
        self._enable(min_samples=20)
        seeded = _history(60, "mixed", ta=0.9, sentiment=0.1, macro=0.1)
        signal = self._signal("sig_exclu", **self.MACRO_ROW)
        fake, result = self._run(signal, "lost", seeded)

        gd = result["gradient_descent"]
        self.assertIsNotNone(gd)
        self.assertEqual(gd["excluded_signal_id"], "sig_exclu")
        self.assertEqual(gd["samples"], 60)
        self.assertEqual(
            gd["weights"],
            list(al.learn_weights_from_history(seeded, min_samples=20)),
        )
        # Et cette ligne écartée aurait bel et bien déplacé les poids — sans quoi
        # l'exclusion serait un raffinement gratuit (voir le test suivant).
        leaky = al.learn_weights_from_history(
            seeded + [{**self.MACRO_ROW, "asset": "EURUSD", "outcome": "lost"}],
            min_samples=20,
        )
        self.assertNotEqual(gd["weights"], list(leaky))

    def test_including_it_would_have_changed_the_weights(self):
        """La preuve que l'exclusion sert à quelque chose, sur la fonction pure.

        Une ligne parmi soixante déplace les poids : c'est ce déplacement-là que le
        règlement s'interdit d'appliquer sur lui-même.
        """
        seeded = _history(60, "mixed", ta=0.9, sentiment=0.1, macro=0.1)
        self_row = {**self.MACRO_ROW, "asset": "EURUSD", "outcome": "lost"}
        without = al.learn_weights_from_history(seeded, min_samples=20)
        with_self = al.learn_weights_from_history(seeded + [self_row], min_samples=20)
        self.assertNotEqual(without, with_self)

    def test_every_row_of_the_settled_signal_is_excluded(self):
        """Deux lignes pour un même signal, c'est **un** trade : les deux s'en vont.

        Le règlement est désormais idempotent, donc ce cas ne se crée plus tout seul
        (voir `SettlementIdempotenceTest`) ; il peut en rester dans le tableau —
        lignes antérieures, écriture manuelle. N'en écarter qu'une ferait compter le
        trade deux fois dans l'entraînement qui décide des poids.
        """
        rows = self._dated_history(58, newest_signal_id="sig_double")
        #: Un second règlement du même signal, plus ancien que le premier.
        rows.append(
            {
                "asset": "EURUSD",
                "outcome": "won",
                "signal_id": "sig_double",
                "created_at": "2026-12-30T23:59:59+00:00",
                "ta_score": 0.9,
                "sentiment_score": 0.1,
                "macro_score": 0.1,
            }
        )
        self.client.store("trade_post_mortems").rows = [dict(row) for row in rows]

        window = al._fetch_settled_post_mortems("EURUSD", exclude_signal_id="sig_double")
        self.assertEqual(len(window), 58)
        self.assertNotIn("sig_double", [row.get("signal_id") for row in window])

    def test_an_unknown_identity_is_not_used_to_exclude(self):
        """Le repli `sig_0` n'est pas une identité : exclure sur lui viserait un autre trade.

        Un signal sans identifiant partage `sig_0` avec **tous** les règlements
        sans identifiant : la ligne d'hier ne peut pas être écartée de
        l'entraînement d'aujourd'hui sur ce nom-là. Faute d'identité, la fenêtre
        garde donc la ligne du trade en cours.
        """
        self._enable(min_samples=20)
        seeded = _history(59, "mixed")
        seeded.append(
            {
                "asset": "EURUSD",
                "outcome": "won",
                "signal_id": al.UNKNOWN_SIGNAL_ID,
                "ta_score": 0.5,
                "sentiment_score": 0.5,
                "macro_score": 0.5,
            }
        )
        # Pas d'identifiant : `sig_0` est un repli, pas une identité.
        signal = {"asset": "EURUSD", "direction": "BUY", "confidence": 0.7}
        fake, result = self._run(signal, "lost", seeded)

        gd = result["gradient_descent"]
        self.assertIsNone(gd["excluded_signal_id"])
        # 60 semés **plus** la ligne du règlement : aucune exclusion demandée.
        self.assertEqual(gd["samples"], 61)

    def _dated_history(self, count, *, newest_signal_id=None):
        """Historique horodaté, le trade visé étant le plus récent du tableau.

        `created_at` est **présent partout** à dessein : la doublure range un champ
        absent après les autres en tri descendant, donc sans horodatage la position
        de la ligne écartée serait décidée par la doublure et non par le test — un
        `LIMIT` trop court pourrait alors tomber juste par chance.
        """
        rows = _history(count, "mixed")
        for index, row in enumerate(rows):
            row["created_at"] = f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}+00:00"
        if newest_signal_id is not None:
            rows.append(
                {
                    "asset": "EURUSD",
                    "outcome": "lost",
                    "signal_id": newest_signal_id,
                    "created_at": "2026-12-31T23:59:59+00:00",
                    "ta_score": 0.5,
                    "sentiment_score": 0.5,
                    "macro_score": 0.5,
                }
            )
        return rows

    def test_a_full_window_stays_full_when_one_trade_is_excluded(self):
        """L'exclusion ne raccourcit pas la fenêtre : on lit une ligne de plus."""
        rows = self._dated_history(al.HISTORY_LIMIT + 1, newest_signal_id="sig_fenetre")
        self.client.store("trade_post_mortems").rows = [dict(row) for row in rows]

        window = al._fetch_settled_post_mortems("EURUSD", exclude_signal_id="sig_fenetre")
        self.assertEqual(len(window), al.HISTORY_LIMIT)

    def test_an_absent_identity_does_not_shorten_the_window(self):
        """Un identifiant absent du tableau (règlement ancien) ne vide rien de plus."""
        rows = self._dated_history(30)
        self.client.store("trade_post_mortems").rows = [dict(row) for row in rows]

        window = al._fetch_settled_post_mortems("EURUSD", exclude_signal_id="sig_absent")
        self.assertEqual(len(window), 30)

    def test_the_settled_trade_cannot_grant_the_training_by_itself(self):
        """19 antérieurs et `min_samples=20` : l'heuristique est conservée.

        Avant l'exclusion, ce règlement atteignait le seuil à lui tout seul — la
        ligne qu'il venait d'écrire faisait le 20ᵉ échantillon.
        """
        self._enable(min_samples=20)
        fake, result = self._run(self._signal("sig_20"), "lost", _history(19, "mixed"))

        self.assertEqual(result["weight_update"], "heuristic")
        self.assertIsNone(result["gradient_descent"])
        self.assertTrue(fake.store("adaptive_model_weights").upserted)

    def test_the_twentieth_prior_trade_grants_it(self):
        """Le vingtième **antérieur** déclenche l'apprentissage, et compte pour 20."""
        self._enable(min_samples=20)
        fake, result = self._run(self._signal("sig_21"), "lost", _history(20, "mixed"))

        self.assertEqual(result["weight_update"], "gradient_descent")
        self.assertEqual(result["gradient_descent"]["samples"], 20)


class SettlementIdempotenceTest(AdaptiveLearningGdTestBase):
    """Un trade se règle **une** fois : rejouer le même signal ne compte pas deux fois.

    Le doublon n'est pas théorique : `POST /learning/feedback` peut être rappelé
    (retry d'un client, rejeu manuel), et le tracker peut repasser sur un signal
    dont le verdict n'est pas allé au bout. Un second règlement ajouterait une ligne
    en double — comptée deux fois par la descente de gradient — et incrémenterait une
    seconde fois `total_trades`, `consecutive_losses` et le taux de réussite, la
    mémoire du modèle, que rien ne permettrait plus de démêler ensuite.

    L'identité est le `signal_id` (`pending_signals.id` pour le tracker, le
    `signal_id` de la requête pour la route). Un signal **sans** identifiant n'en a
    pas : le repli `sig_0` est partagé par tous les règlements sans identifiant,
    donc s'en servir pour dire « déjà réglé » interdirait à jamais l'apprentissage
    sur ces signaux-là — c'est vérifié aussi.
    """

    SIGNAL = {"id": "sig-1", "asset": "EURUSD", "direction": "BUY", "confidence": 0.7}

    def _settle_twice(self, outcome="lost", history=()):
        """Le même signal réglé deux fois de suite, sans vider la base entre les deux."""
        self.client.store("trade_post_mortems").rows = [dict(row) for row in history]
        with mock.patch.object(al, "upsert_note", lambda **kwargs: None):
            first = al.record_trade_settlement_and_learn(dict(self.SIGNAL), outcome)
            second = al.record_trade_settlement_and_learn(dict(self.SIGNAL), outcome)
        return first, second

    def test_a_replay_writes_no_second_row(self):
        first, second = self._settle_twice("lost")

        self.assertEqual(first["status"], "learned")
        self.assertEqual(second["status"], "already_settled")
        self.assertEqual(len(self.client.store("trade_post_mortems").rows), 1)

    def test_a_replay_writes_no_second_weight_update(self):
        """Ni ligne, ni compteur : un seul `upsert` de poids, et il date du premier."""
        first, second = self._settle_twice("lost")

        upserts = self.client.store("adaptive_model_weights").upserted
        self.assertEqual(len(upserts), 1, "le rejeu ne doit pas réécrire les poids")
        self.assertEqual(upserts[0][0]["total_trades"], 1)
        self.assertEqual(upserts[0][0]["consecutive_losses"], 1)
        self.assertEqual(second["weight_update"], "unchanged")
        self.assertIsNone(second["gradient_descent"])
        #: Et les paramètres annoncés sont ceux **en vigueur**, pas des valeurs
        #: recalculées : une perte de plus se verrait ici.
        self.assertEqual(second["updated_params"]["consecutive_losses"], 1)
        self.assertEqual(
            second["updated_params"]["win_rate"], first["updated_params"]["win_rate"]
        )

    def test_a_replay_says_what_was_already_settled(self):
        """Le retour dit le premier règlement, et il dit la même leçon.

        Le diagnostic est recalculé — c'est une fonction pure du même signal —, donc
        l'appelant qui n'affiche que la leçon (le tracker) continue d'afficher
        quelque chose de vrai, tout en sachant que rien n'a été appris.
        """
        first, second = self._settle_twice("lost")

        self.assertEqual(second["duplicate_of"]["signal_id"], "sig-1")
        self.assertEqual(second["duplicate_of"]["outcome"], "lost")
        self.assertEqual(second["post_mortem"]["lesson"], first["post_mortem"]["lesson"])

    def test_a_replay_of_the_opposite_outcome_is_still_a_replay(self):
        """Le premier verdict fait foi : un rejeu qui le contredit n'est pas une correction.

        Le tracker apprend « perdu » alors que la route avait appris « gagné » : c'est
        le **même** trade, donc un rejeu — pas un second trade. Reconnaître le doublon
        par l'identité **seule** est ce qui l'empêche d'écrire ; s'appuyer sur l'issue
        (« un règlement « perdu » n'est un doublon que d'un règlement « perdu » »)
        rendrait la mémoire du modèle dépendante de qui parle en dernier : le tracker
        repasse tant que son verdict n'est pas allé au bout, il écraserait un
        règlement manuel, et inversement. Le retour dit donc les deux issues — celle
        qui est enregistrée (`duplicate_of`) et celle qu'on proposait (`outcome`).
        """
        self.client.store("trade_post_mortems").rows = []
        with mock.patch.object(al, "upsert_note", lambda **kwargs: None):
            first = al.record_trade_settlement_and_learn(dict(self.SIGNAL), "won")
            second = al.record_trade_settlement_and_learn(dict(self.SIGNAL), "lost")

        self.assertEqual(first["status"], "learned")
        self.assertEqual(second["status"], "already_settled")
        self.assertEqual(second["duplicate_of"]["outcome"], "won")
        self.assertEqual(second["outcome"], "lost")
        rows = self.client.store("trade_post_mortems").rows
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "won", "le verdict enregistré ne change pas")
        self.assertEqual(self.client.store("adaptive_model_weights").upserted[0][0]["total_trades"], 1)

    def test_a_replay_does_not_refresh_the_lessons_note(self):
        """Rien n'a changé dans l'historique : la note n'a rien de neuf à dire."""
        self.client.store("trade_post_mortems").rows = []
        with mock.patch.object(al, "upsert_note") as note:
            al.record_trade_settlement_and_learn(dict(self.SIGNAL), "lost")
            al.record_trade_settlement_and_learn(dict(self.SIGNAL), "lost")
        self.assertEqual(note.call_count, 1)

    def test_two_settled_signals_both_count(self):
        """L'idempotence n'est pas une surdité : deux trades font deux règlements."""
        self.client.store("trade_post_mortems").rows = []
        with mock.patch.object(al, "upsert_note", lambda **kwargs: None):
            first = al.record_trade_settlement_and_learn(dict(self.SIGNAL), "won")
            other = {**self.SIGNAL, "id": "sig-2"}
            second = al.record_trade_settlement_and_learn(other, "lost")

        self.assertEqual(first["status"], "learned")
        self.assertEqual(second["status"], "learned")
        self.assertEqual(len(self.client.store("trade_post_mortems").rows), 2)
        upserts = self.client.store("adaptive_model_weights").upserted
        self.assertEqual([row[0]["total_trades"] for row in upserts], [1, 2])

    def test_a_signal_without_identity_is_settled_every_time(self):
        """Faute d'identité, on **ne peut pas** reconnaître un rejeu — et on ne devine pas.

        Traiter `sig_0` comme une identité ferait du premier règlement sans
        identifiant le « déjà réglé » de tous les suivants : ces signaux ne seraient
        plus jamais appris. Mieux vaut compter deux fois un signal qu'on ne sait pas
        nommer que de n'apprendre d'aucun.
        """
        self.client.store("trade_post_mortems").rows = []
        signal = {"asset": "EURUSD", "direction": "BUY", "confidence": 0.7}
        with mock.patch.object(al, "upsert_note", lambda **kwargs: None):
            first = al.record_trade_settlement_and_learn(dict(signal), "won")
            second = al.record_trade_settlement_and_learn(dict(signal), "won")

        self.assertEqual([first["status"], second["status"]], ["learned", "learned"])
        self.assertEqual(len(self.client.store("trade_post_mortems").rows), 2)

    def test_an_unreadable_check_settles_anyway_and_says_so(self):
        """Une lecture de contrôle qui échoue ne doit pas perdre un vrai règlement.

        Le doublon, lui, est au moins journalisé — et il se répare : une ligne de
        trop se supprime, un trade jamais appris ne se voit nulle part. Le choix est
        donc assumé, mais pas silencieux.
        """
        self._enable(min_samples=20)  # le flag est hors sujet ici : la lecture est la même
        self.client.store("trade_post_mortems").rows = []
        self.client.fail(
            "trade_post_mortems",
            "select",
            RuntimeError("trade_post_mortems: lecture refusée"),
        )
        with mock.patch.object(al, "upsert_note", lambda **kwargs: None):
            with self.assertLogs(al.logger, level="WARNING") as logs:
                result = al.record_trade_settlement_and_learn(dict(self.SIGNAL), "lost")

        self.assertEqual(result["status"], "learned")
        self.assertEqual(len(self.client.store("trade_post_mortems").rows), 1)
        self.assertIn("Vérification de doublon indisponible", "\n".join(logs.output))


class ExclusionDocumentedTest(unittest.TestCase):
    """La décision se lit là où on la cherche : `docs/ADAPTIVE_LEARNING.md`.

    Une exclusion non écrite se relit comme un oubli — « pourquoi 60 échantillons
    pour 61 lignes ? » — et la première réparation venue serait de la retirer.
    """

    DOC = pathlib.Path(__file__).resolve().parents[1] / "docs" / "ADAPTIVE_LEARNING.md"

    def test_the_decision_is_documented(self):
        doc = self.DOC.read_text(encoding="utf-8")
        self.assertIn("n'entre pas dans son propre entraînement", doc)
        self.assertIn("excluded_signal_id", doc)
        self.assertIn("se compte **après** l'exclusion", doc)


# --------------------------------------------------------------------------- #
# 4. Fonctions pures : bornage, seuil minimal, robustesse
# --------------------------------------------------------------------------- #


class PureLearningTest(unittest.TestCase):
    def test_ignores_unsettled_outcomes(self):
        rows = _history(50, "mixed") + [{"outcome": "open", "ta_score": 0.9}]
        self.assertIsNotNone(al.learn_weights_from_history(rows, min_samples=20))

    def test_returns_none_below_min_samples(self):
        self.assertIsNone(al.learn_weights_from_history(_history(19, "mixed"), min_samples=20))
        self.assertIsNotNone(al.learn_weights_from_history(_history(20, "mixed"), min_samples=20))

    def test_empty_history_is_safe(self):
        self.assertIsNone(al.learn_weights_from_history([], min_samples=5))
        self.assertIsNone(al.learn_weights_from_history(None, min_samples=5))

    def test_learned_weights_respect_safety_bounds(self):
        """Même avec un signal parfaitement séparable, aucun facteur ne capte tout."""
        weights = al.learn_weights_from_history(_history(200, "won", 1.0, 0.0, 0.0), min_samples=20)
        self.assertIsNotNone(weights)
        for w in weights:
            self.assertGreaterEqual(w, al.MIN_FACTOR_WEIGHT - 1e-9)
            self.assertLessEqual(w, al.MAX_FACTOR_WEIGHT + 1e-9)
        self.assertAlmostEqual(sum(weights), 1.0, places=6)

    def test_no_signal_data_keeps_prior(self):
        """Des labels sans signal ne doivent pas éloigner des poids d'équilibre."""
        rows = [{"outcome": "won" if i % 2 else "lost"} for i in range(60)]
        weights = al.learn_weights_from_history(rows, min_samples=20)
        self.assertIsNotNone(weights)
        for got, prior in zip(weights, al.EQUILIBRIUM_WEIGHTS):
            self.assertAlmostEqual(got, prior, delta=0.05)

    def test_fit_is_deterministic(self):
        rows = _history(60, "mixed")
        first = al.learn_weights_from_history(rows, min_samples=20)
        second = al.learn_weights_from_history(rows, min_samples=20)
        self.assertEqual(first, second)

    def test_clamp_factor_weights_renormalizes(self):
        clamped = al.clamp_factor_weights([0.9, 0.05, 0.05])
        self.assertIsNotNone(clamped)
        self.assertAlmostEqual(sum(clamped), 1.0, places=9)
        self.assertLessEqual(clamped[0], al.MAX_FACTOR_WEIGHT + 1e-9)

    def test_clamp_factor_weights_rejects_degenerate_input(self):
        self.assertIsNone(al.clamp_factor_weights([0.0, 0.0, 0.0]))
        self.assertIsNone(al.clamp_factor_weights([-1.0, 0.0, 0.0]))

    def test_extract_subscore_row_clamps_out_of_range(self):
        row = al.extract_subscore_row({"ta_score": 5.0, "sentiment_score": -2.0})
        self.assertEqual(row, (1.0, 0.0, 0.5))


# --------------------------------------------------------------------------- #
# 5. Lecture du flag depuis l'environnement (défaut sûr + bornage)
# --------------------------------------------------------------------------- #


class GdFlagConfigTest(AdaptiveLearningGdTestBase):
    def test_min_samples_is_bounded_low(self):
        from core.config_runtime import get_env_config

        for raw in ("0", "-5", "1"):
            os.environ["ADAPTIVE_GD_MIN_SAMPLES"] = raw
            reset_env_config()
            self.assertEqual(get_env_config().adaptive_gd_min_samples, 2)

    def test_enabled_flag_accepts_truthy_values(self):
        from core.config_runtime import get_env_config

        os.environ["ADAPTIVE_GD_ENABLED"] = "yes"
        reset_env_config()
        self.assertTrue(get_env_config().adaptive_gd_enabled)

        os.environ["ADAPTIVE_GD_ENABLED"] = "no"
        reset_env_config()
        self.assertFalse(get_env_config().adaptive_gd_enabled)

    def test_corrupt_value_falls_back_to_off(self):
        """Un flag illisible ne doit jamais activer l'apprentissage par accident."""
        from core.config_runtime import get_env_config

        os.environ["ADAPTIVE_GD_ENABLED"] = "maybe"
        reset_env_config()
        self.assertFalse(get_env_config().adaptive_gd_enabled)


# --------------------------------------------------------------------------- #
# 6. Bout en bout : le moteur produit bien les sous-notes, et elles survivent
#    jusqu'à la persistance (sinon l'entraînement porterait sur du vide).
# --------------------------------------------------------------------------- #


class DecisionEngineSubscoresTest(unittest.TestCase):
    def _signal(self):
        from core.signal_quality import normalize_signal

        from ai import decision_engine as de

        engine = de.EmotionlessDecisionEngine(min_conf=0.55)
        def _no_llm(asset, insights, regime=None, extra_context=None):
            """Repli sans LLM. `extra_context` fait partie du contrat réel : le
            moteur le transmet depuis la boucle RAG (`/use`)."""
            return None

        engine._news_cache.get = _no_llm
        closes = [1.10 + i * 0.0005 for i in range(60)]
        patches = [
            mock.patch.object(de, "get_adaptive_parameters", lambda asset: {
                "ta_weight": 0.40,
                "sentiment_weight": 0.30,
                "macro_weight": 0.30,
                "min_confidence": 0.58,
                "sl_multiplier": 1.5,
                "tp_multiplier": 3.0,
                "consecutive_losses": 0,
                "win_rate": 50.0,
                "total_trades": 0,
                "active_rules": [],
            }),
            mock.patch.object(de, "get_economic_events", lambda limit=60: []),
            mock.patch.object(de, "check_news_risk", lambda asset, events: {
                "allowed": True,
                "risk_level": "Low",
                "reason": "",
                "penalty": 0.0,
            }),
            mock.patch.object(de, "get_currencies_for_symbol", lambda asset: ["EUR", "USD"]),
            mock.patch.object(de, "calculate_symbol_macro_bias", lambda asset, events: (0.4, "Bullish", ["stub"])),
            mock.patch.object(de, "get_closes", lambda asset, interval="1h": closes),
            mock.patch.object(de, "get_recent_insights", lambda limit=20: []),
            mock.patch.object(de, "log_macro_decision", lambda **kwargs: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        # Conditions TA déterministes : RSI survendu + EMA20 > EMA50.
        engine._rsi = lambda closes_, period=14: 25.0
        engine._ema = lambda closes_, period: [1.0 if period == 20 else 0.9]

        raw = engine.analyze("EURUSD")
        self.assertIsNotNone(raw, "le signal de test doit être produit")
        normalized, _ = normalize_signal(raw, allow_demo=True)
        return raw, normalized

    def test_engine_exposes_the_three_subscores(self):
        raw, _ = self._signal()
        for field in al.SUBSCORE_FIELDS:
            self.assertIn(field, raw)
            self.assertGreaterEqual(raw[field], 0.0)
            self.assertLessEqual(raw[field], 1.0)
        # Valeurs attendues du scénario : TA saturé, sentiment = moyenne du repli
        # sans LLM ((géo 0.35 + sentiment 0.50) / 2), macro 0.4 -> 0.64.
        self.assertEqual(raw["ta_score"], 1.0)
        self.assertAlmostEqual(raw["sentiment_score"], 0.425, places=4)
        self.assertAlmostEqual(raw["macro_score"], 0.64, places=4)

    def test_subscores_survive_normalization_and_reach_the_post_mortem(self):
        raw, normalized = self._signal()
        self.assertEqual(al.extract_subscore_row(normalized), al.extract_subscore_row(raw))

        client = supabase_double.use_supabase(
            self, supabase_double.SupabaseDouble(), al
        )
        with mock.patch.object(al, "upsert_note", lambda **kwargs: None):
            al.record_trade_settlement_and_learn(normalized, "lost")

        row = client.store("trade_post_mortems").inserted[0]
        self.assertAlmostEqual(row["ta_score"], 1.0, places=4)
        self.assertAlmostEqual(row["macro_score"], 0.64, places=4)
        self.assertAlmostEqual(row["sentiment_score"], 0.425, places=4)
        # La valeur historique codée en dur ne doit plus apparaître partout.
        self.assertNotEqual((row["ta_score"], row["sentiment_score"], row["macro_score"]), (0.5, 0.5, 0.5))


if __name__ == "__main__":
    unittest.main()
