import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple
from datetime import datetime, timezone
from database.supabase_client import supabase
from database.knowledge_base import upsert_note
from core.config_runtime import get_env_config
from core.gradient_descent import fit_simplex_weights

logger = logging.getLogger(__name__)

LESSONS_SOURCE = "performance:adaptive_learning"

# Poids d'équilibre : point de départ ET cible de la régularisation L2.
EQUILIBRIUM_WEIGHTS: Tuple[float, float, float] = (0.40, 0.30, 0.30)

# Variables explicatives du modèle (ordre = ordre des poids).
SUBSCORE_FIELDS: Tuple[str, ...] = ("ta_score", "sentiment_score", "macro_score")

# Sous-note neutre quand un signal plus ancien n'a pas persisté ses sous-notes.
NEUTRAL_SUBSCORE = 0.5

# Bornes de sûreté : aucun facteur ne doit capter tout le poids (0.0 et 1.0 sont
# inatteignables avec softmax, mais un modèle saturé reste dangereux en trading).
MIN_FACTOR_WEIGHT = 0.10
MAX_FACTOR_WEIGHT = 0.60

# Par défaut la descente de gradient est INACTIVE (voir ADAPTIVE_GD_ENABLED).
DEFAULT_GD_MIN_SAMPLES = 20

#: Taille de la fenêtre d'entraînement : les `HISTORY_LIMIT` derniers règlements
#: de l'actif, le trade en cours excepté (voir `_fetch_settled_post_mortems`).
HISTORY_LIMIT = 200

#: Identifiant de repli d'un signal qui n'en porte pas. **Ce n'est pas une
#: identité** : deux règlements sans identifiant le partagent, donc il ne peut pas
#: servir à reconnaître le trade qu'on règle au milieu de l'historique (voir
#: `record_trade_settlement_and_learn`, étape 2bis).
UNKNOWN_SIGNAL_ID = "sig_0"


def _subscore(value: Any, default: float = NEUTRAL_SUBSCORE) -> float:
    """Sous-note bornée à [0, 1], avec repli neutre si absente ou non numérique."""
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _signal_identity(signal_data: Dict[str, Any]) -> Optional[str]:
    """L'identifiant du trade réglé, ou `None` si le signal n'en porte pas.

    `UNKNOWN_SIGNAL_ID` est un **repli**, pas une identité : tous les règlements
    sans identifiant le partagent, donc s'en servir reviendrait à traiter le
    premier d'entre eux comme « déjà réglé » pour tous les suivants — et à
    interdire à jamais l'apprentissage sur les signaux qui n'en portent pas.
    Deux usages, une seule règle : tenir le trade hors de son propre entraînement
    (`_fetch_settled_post_mortems`) et refuser de le régler deux fois
    (`_settled_before`).
    """
    identity = str(signal_data.get("id") or "").strip()
    return identity or None


def _settled_before(signal_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Le premier règlement de ce signal, s'il a déjà eu lieu.

    Un trade se règle **une** fois. `trade_post_mortems` est en ajout seul, son
    diagnostic est dérivé de (signal, issue), et ses compteurs
    (`adaptive_model_weights`) aussi : un second règlement du même signal
    n'apporte donc aucune information — il ajoute une ligne en double, qui
    comptera deux fois dans l'historique de la descente de gradient, et il
    incrémente une seconde fois `total_trades`, `consecutive_losses` et le taux de
    réussite, c'est-à-dire la mémoire du modèle. Le doublon vient des rejeux :
    `POST /learning/feedback` rappelé (retry d'un client, rejeu manuel), ou le
    tracker qui repasse sur un signal dont le verdict n'est pas allé au bout.

    `signal_id` est l'identité du trade (`pending_signals.id`, ou le `signal_id` de
    la requête). Un signal **sans** identité ne peut pas être reconnu : rien ne dit
    qu'un règlement sans identifiant est le même qu'un autre (voir
    `_signal_identity`), et c'est dit plutôt que deviné.

    Si la lecture échoue, on **règle quand même** : perdre un règlement réel parce
    qu'une lecture a hoqueté coûterait plus que le doublon qu'elle évite — un trade
    jamais appris ne se voit nulle part, alors qu'un doublon est au moins journalisé
    ici. L'avertissement le dit, il n'y a donc pas de silence.
    """
    if not supabase or not signal_id:
        return None
    try:
        res = (
            supabase.table("trade_post_mortems")
            .select("signal_id,outcome,learned_lesson,created_at")
            .eq("signal_id", signal_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.warning(
            "Vérification de doublon indisponible pour %s, règlement appliqué : %s",
            signal_id,
            e,
        )
        return None
    rows = res.data or []
    return rows[0] if rows else None


def extract_subscore_row(signal_data: Dict[str, Any]) -> Tuple[float, float, float]:
    """Sous-notes (TA, sentiment, macro) réellement utilisées dans le signal.

    Elles sont produites par `ai/decision_engine.py` et transportées telles
    quelles jusqu'ici (les clés inconnues survivent à `normalize_signal`).
    Un signal ancien qui ne les porte pas retombe sur la valeur neutre.
    """
    return tuple(_subscore(signal_data.get(field)) for field in SUBSCORE_FIELDS)  # type: ignore[return-value]


def clamp_factor_weights(
    weights: Sequence[float],
    low: float = MIN_FACTOR_WEIGHT,
    high: float = MAX_FACTOR_WEIGHT,
    tol: float = 1e-12,
) -> Optional[Tuple[float, ...]]:
    """Projette les poids sur le simplexe **borné** {somme = 1, low <= w <= high}.

    Un simple écrêtage suivi d'une renormalisation ne suffit pas : il ramène la
    coordonnée au-dessus de la borne (l'excédent redistribué resort de l'intervalle).
    Ici l'excédent est redistribué proportionnellement à la marge restante de
    chaque coordonnée libre, ce qui ne peut jamais dépasser une borne.

    Retourne `None` si le problème est infaisable (contraintes contradictoires).
    """
    n = len(weights)
    if n == 0:
        return None
    total = sum(weights)
    if total <= 0 or low < 0 or high < low:
        return None
    if low * n > 1.0 or high * n < 1.0:
        return None  # aucune distribution ne peut respecter les bornes

    w = [min(max(x / total, low), high) for x in weights]
    for _ in range(2 * n + 4):
        deficit = 1.0 - sum(w)
        if abs(deficit) <= tol:
            break
        margins = [high - x for x in w] if deficit > 0 else [x - low for x in w]
        capacity = sum(m for m in margins if m > 0)
        if capacity <= tol:
            break  # plus aucune marge : bornes saturées
        amount = min(abs(deficit), capacity)
        direction = 1.0 if deficit > 0 else -1.0
        for i, margin in enumerate(margins):
            if margin > 0:
                w[i] += direction * amount * (margin / capacity)

    total = sum(w)
    if total <= 0:
        return None
    # Correction de dernier recours (~1e-15), sans effet sur les bornes.
    return tuple(min(max(x / total, low), high) for x in w)


def learn_weights_from_history(
    post_mortems: Sequence[Dict[str, Any]],
    min_samples: int = DEFAULT_GD_MIN_SAMPLES,
    prior_weights: Sequence[float] = EQUILIBRIUM_WEIGHTS,
    learning_rate: float = 0.5,
    epochs: int = 500,
    l2: float = 1e-3,
) -> Optional[Tuple[float, ...]]:
    """Apprend les poids (TA, sentiment, macro) par descente de gradient.

    Fonction **pure** : aucun accès réseau, donc directement testable. Elle
    n'utilise que les trades effectivement réglés (`won`/`lost`) porteurs de
    leurs sous-notes. Retourne `None` si l'historique est trop court pour que
    l'estimation soit autre chose que du bruit — l'appelant conserve alors son
    comportement heuristique.

    La **fenêtre** est celle de l'appelant : cette fonction ne sait pas quel trade
    est en train d'être réglé, elle ne peut donc pas l'écarter elle-même. Il lui
    faut un historique où le trade en cours est **déjà** absent — sans quoi les
    poids écrits à l'occasion de son règlement seraient ajustés sur l'issue qu'ils
    servent à expliquer. C'est `_gradient_descent_weights` qui s'en charge, et par
    identifiant (`_fetch_settled_post_mortems`).
    """
    X: List[List[float]] = []
    y: List[float] = []
    for row in post_mortems or []:
        outcome = str(row.get("outcome") or "").strip().lower()
        if outcome not in ("won", "lost"):
            continue
        X.append([_subscore(row.get(field)) for field in SUBSCORE_FIELDS])
        y.append(1.0 if outcome == "won" else 0.0)

    if len(y) < max(2, int(min_samples)):
        return None

    fit = fit_simplex_weights(
        X,
        y,
        prior_weights=list(prior_weights),
        learning_rate=learning_rate,
        epochs=epochs,
        l2=l2,
    )
    if not fit.weights or len(fit.weights) != len(SUBSCORE_FIELDS):
        return None
    return clamp_factor_weights(fit.weights)


def _fetch_settled_post_mortems(
    asset: str,
    limit: int = HISTORY_LIMIT,
    exclude_signal_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Historique réglé d'un actif, sous-notes incluses (vide si indisponible).

    `exclude_signal_id` écarte le trade qu'on est en train de régler : sa ligne
    vient d'être insérée dans `trade_post_mortems` (étape 1 du règlement), elle
    serait donc le premier échantillon du fit qui décide des poids écrits dans la
    foulée. **Un trade ne doit pas entrer dans son propre entraînement** : le
    modèle ajusterait son adaptation sur l'issue qu'elle est censée expliquer, et
    ce trade-là pèserait d'autant plus lourd que l'historique est court.

    Trois choix, chacun pour une raison :

    * **par identifiant, jamais par date** — `created_at` est posé par le serveur,
      deux règlements tombent dans la même seconde, et « la ligne la plus
      récente » serait une supposition : elle retirerait la ligne de quelqu'un
      d'autre. L'identifiant du signal est la seule chose qui désigne ce trade-là ;
    * **toutes les lignes de cet identifiant sont écartées** — un signal réglé deux
      fois (retour `/learning/feedback` rejoué, rattrapage du tracker) a deux
      lignes pour **un** trade : les garder toutes les deux le compteraient deux
      fois dans l'échantillon ;
    * **la fenêtre ne se raccourcit pas** quand l'exclusion ne trouve rien — on lit
      `limit + 1` lignes, donc un `limit` plein de trades **antérieurs** au
      règlement ; si l'identifiant n'est pas dans le tableau (règlement ancien,
      insertion ratée), la fenêtre reste celle des `limit` derniers règlements.

    Une ligne **sans** `signal_id` n'est jamais écartée : elle ne dit pas de quel
    trade elle vient, donc rien ne permet d'affirmer que c'est celui-ci.
    """
    if not supabase:
        return []
    #: Une ligne de plus que la fenêtre voulue : l'exclusion ne doit pas coûter un
    #: trade antérieur, qui a sa place dans l'entraînement.
    wanted = limit + 1 if exclude_signal_id is not None else limit
    try:
        res = (
            supabase.table("trade_post_mortems")
            .select("signal_id,outcome,ta_score,sentiment_score,macro_score")
            .eq("asset", asset)
            .order("created_at", desc=True)
            .limit(wanted)
            .execute()
        )
        rows = res.data or []
    except Exception as e:
        logger.warning(f"Historique post-mortem indisponible pour {asset}: {e}")
        return []

    if exclude_signal_id is None:
        return rows
    excluded = str(exclude_signal_id)
    kept = [row for row in rows if str(row.get("signal_id") or "") != excluded]
    return kept[:limit]


def _gradient_descent_weights(
    asset: str, exclude_signal_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Poids appris, ou `None` si le flag est inactif ou l'historique insuffisant.

    `exclude_signal_id` est le trade en train d'être réglé : il est tenu hors de
    l'échantillon **avant** que le seuil ne soit appliqué, donc il ne peut pas non
    plus *accorder* l'entraînement à lui tout seul — 19 trades antérieurs, un
    règlement, `ADAPTIVE_GD_MIN_SAMPLES=20` : l'heuristique est conservée, et la
    descente de gradient se déclenche au règlement suivant (20 trades antérieurs).
    """
    cfg = get_env_config()
    if not cfg.adaptive_gd_enabled:
        return None

    min_samples = int(getattr(cfg, "adaptive_gd_min_samples", DEFAULT_GD_MIN_SAMPLES))
    history = _fetch_settled_post_mortems(asset, exclude_signal_id=exclude_signal_id)
    learned = learn_weights_from_history(history, min_samples=min_samples)
    if learned is None:
        logger.info(
            "Descente de gradient ignorée pour %s : %d trades réglés%s < %d requis",
            asset,
            len(history),
            f" hors {exclude_signal_id}" if exclude_signal_id else "",
            min_samples,
        )
        return None

    return {
        "samples": len(history),
        "min_samples": min_samples,
        #: Le trade que ce règlement a tenu hors de son propre entraînement —
        #: `None` quand le signal n'avait pas d'identifiant (voir UNKNOWN_SIGNAL_ID).
        "excluded_signal_id": exclude_signal_id,
        # Pleine précision : l'arrondi n'est appliqué qu'à la persistance.
        "weights": [float(w) for w in learned],
    }


def analyze_trade_error(signal: Dict[str, Any], outcome: str, exit_price: Optional[float] = None) -> Dict[str, Any]:
    """
    Perform an automated post-mortem diagnosis on a settled trade.
    Identifies root causes for lost trades and produces a corrective action lesson.
    """
    asset = signal.get("asset", "UNKNOWN")
    direction = signal.get("direction", "BUY")
    confidence = float(signal.get("confidence") or 0.5)
    ta_summary = str(signal.get("ta_summary") or "")
    geo_summary = str(signal.get("geo_summary") or "")
    entry_price = float(signal.get("price") or 0.0)

    if outcome == "won":
        return {
            "error_type": "none",
            "lesson": f"✅ Trade gagné sur {asset} ({direction}) : Configuration TA + Macro confirmée. Modèle validé.",
            "corrective_action": "maintain_weights"
        }

    # Diagnosis for lost trade
    error_type = "general_market_noise"
    lesson = f"⚠️ Trade perdu sur {asset} ({direction})."
    corrective_action = "stricter_threshold"

    if "RSI=" in ta_summary:
        try:
            rsi_val = float(ta_summary.split("RSI=")[1].split()[0])
            if direction == "BUY" and rsi_val > 68:
                error_type = "overbought_entry"
                lesson = f"🛑 Achats en surachat (RSI {rsi_val:.1f}) ont échoué sur {asset}. Eviter les entées BUY quand RSI > 65."
                corrective_action = "require_lower_rsi"
            elif direction == "SELL" and rsi_val < 32:
                error_type = "oversold_entry"
                lesson = f"🛑 Ventes en survente (RSI {rsi_val:.1f}) ont échoué sur {asset}. Eviter les entées SELL quand RSI < 35."
                corrective_action = "require_higher_rsi"
        except Exception:
            pass

    if "BEARISH" in geo_summary and direction == "BUY":
        error_type = "macro_divergence"
        lesson = f"🛑 Achat de {asset} exécuté contre un biais Macro Bearish. Relever le poids de la Macro dans l'équation."
        corrective_action = "boost_macro_weight"
    elif "BULLISH" in geo_summary and direction == "SELL":
        error_type = "macro_divergence"
        lesson = f"🛑 Vente de {asset} exécutée contre un biais Macro Bullish. Eviter la contre-tendance Macro."
        corrective_action = "boost_macro_weight"

    if exit_price and entry_price > 0:
        price_diff_pct = abs(exit_price - entry_price) / entry_price * 100.0
        if price_diff_pct < 0.6:
            error_type = "tight_stop_loss"
            lesson = f"⚠️ Stop loss trop serré sur {asset} ({price_diff_pct:.2f}%). Élargir le multiplicateur ATR de 1.5 à 1.8."
            corrective_action = "expand_sl_atr"

    return {
        "error_type": error_type,
        "lesson": lesson,
        "corrective_action": corrective_action
    }


def get_adaptive_parameters(asset: str) -> Dict[str, Any]:
    """
    Retrieve or compute dynamic adaptive parameters (weights, thresholds, SL/TP multipliers) for an asset.
    """
    # Default parameters
    default_params = {
        "asset": asset,
        "ta_weight": 0.40,
        "sentiment_weight": 0.30,
        "macro_weight": 0.30,
        "min_confidence": 0.58,
        "sl_multiplier": 1.5,
        "tp_multiplier": 3.0,
        "consecutive_losses": 0,
        "win_rate": 50.0,
        "total_trades": 0,
        "active_rules": []
    }

    if not supabase:
        return default_params

    try:
        res = supabase.table("adaptive_model_weights").select("*").eq("asset", asset).execute()
        if res and res.data:
            row = res.data[0]
            consec_losses = int(row.get("consecutive_losses", 0))
            win_rate = float(row.get("win_rate_pct", 50.0))
            
            # Dynamic confidence threshold adjustment based on consecutive losses
            base_conf = 0.58
            if consec_losses >= 3:
                base_conf = 0.68  # Much stricter threshold after 3 losses
            elif consec_losses >= 2:
                base_conf = 0.63
            elif win_rate >= 70.0:
                base_conf = 0.55  # Lower threshold when winning streak is active

            rules = []
            if consec_losses >= 2:
                rules.append(f"Seuil de confiance sur-élevé à {base_conf:.2f} suite à {consec_losses} pertes consécutives.")
            if float(row.get("macro_weight", 0.30)) > 0.35:
                rules.append("Poids Macro renforcé suite à des divergences passées.")

            return {
                "asset": asset,
                "ta_weight": float(row.get("ta_weight", 0.40)),
                "sentiment_weight": float(row.get("sentiment_weight", 0.30)),
                "macro_weight": float(row.get("macro_weight", 0.30)),
                "min_confidence": base_conf,
                "sl_multiplier": float(row.get("sl_atr_multiplier", 1.5)),
                "tp_multiplier": float(row.get("tp_atr_multiplier", 3.0)),
                "consecutive_losses": consec_losses,
                "win_rate": win_rate,
                "total_trades": int(row.get("total_trades", 0)),
                "active_rules": rules
            }
    except Exception as e:
        logger.warning(f"Error fetching adaptive parameters for {asset}: {e}")

    return default_params


def record_trade_settlement_and_learn(signal_data: Dict[str, Any], outcome: str, exit_price: Optional[float] = None) -> Dict[str, Any]:
    """
    Called when a trade is settled ('won' or 'lost').
    Analyzes outcome, stores post-mortem, recalibrates model weights & updates knowledge base notes.

    Le règlement est **idempotent par identité de signal** : rejouer le même
    signal ne réécrit ni le post-mortem ni les compteurs, et le retour le dit
    (`status: "already_settled"` — voir `_settled_before`).
    """
    asset = signal_data.get("asset", "UNKNOWN")
    sig_id = str(signal_data.get("id") or UNKNOWN_SIGNAL_ID)
    direction = signal_data.get("direction", "BUY")

    post_mortem = analyze_trade_error(signal_data, outcome, exit_price)

    #: L'identité du trade, s'il en porte une : c'est elle qui le tient hors de son
    #: propre entraînement (étape 2bis) et qui refuse de le régler deux fois (ci-
    #: dessous).
    identity = _signal_identity(signal_data)
    exclude_signal_id = identity

    # Sous-notes réellement utilisées dans le calcul du signal. Elles sont
    # persistées telles quelles : sans elles, le gradient des poids par rapport
    # aux facteurs est identiquement nul (on n'entraînerait que le biais).
    ta_score, sentiment_score, macro_score = extract_subscore_row(signal_data)

    # 0. Ce signal a-t-il déjà été réglé ? (voir `_settled_before`)
    #
    # L'ordre compte : on sort **avant** l'insertion et avant tout compteur. Un
    # rejeu ne dit rien de nouveau sur le trade, mais il dirait deux fois la même
    # chose à `trade_post_mortems` et à `adaptive_model_weights` — dont
    # `total_trades` et `consecutive_losses`, que rien ne permettrait plus de
    # démêler ensuite. Le diagnostic (`post_mortem`), lui, est recalculé : c'est
    # une fonction pure du même signal, donc le retour dit la même leçon, et
    # l'appelant qui n'affiche qu'elle continue d'afficher quelque chose de vrai.
    #
    # La note du savoir (`_refresh_knowledge_base_lessons`) n'est pas réécrite non
    # plus : rien n'a changé dans l'historique qu'elle résume.
    already = _settled_before(identity)
    if already is not None:
        current = get_adaptive_parameters(asset)
        logger.info(
            "Règlement ignoré pour %s (%s) : déjà réglé (%s) — aucune ligne, compteurs inchangés",
            asset,
            sig_id,
            already.get("outcome"),
        )
        return {
            "status": "already_settled",
            "asset": asset,
            "outcome": outcome,
            "post_mortem": post_mortem,
            "subscores": {
                "ta": round(ta_score, 4),
                "sentiment": round(sentiment_score, 4),
                "macro": round(macro_score, 4),
            },
            #: Ni descente de gradient ni heuristique : rien n'est réécrit, donc il
            #: n'y a pas d'« apprentissage » à annoncer.
            "weight_update": "unchanged",
            "gradient_descent": None,
            "duplicate_of": {
                "signal_id": sig_id,
                "outcome": already.get("outcome"),
                "learned_lesson": already.get("learned_lesson"),
                "created_at": already.get("created_at"),
            },
            "updated_params": {
                "ta_weight": current["ta_weight"],
                "sentiment_weight": current["sentiment_weight"],
                "macro_weight": current["macro_weight"],
                "consecutive_losses": current["consecutive_losses"],
                "win_rate": current["win_rate"],
            },
        }

    # 1. Store in trade_post_mortems
    if supabase:
        try:
            supabase.table("trade_post_mortems").insert({
                "signal_id": sig_id,
                "asset": asset,
                "direction": direction,
                "outcome": outcome,
                "entry_price": signal_data.get("price"),
                "exit_price": exit_price,
                "pnl": signal_data.get("pnl", 0.0),
                "ta_score": round(ta_score, 4),
                "sentiment_score": round(sentiment_score, 4),
                "macro_score": round(macro_score, 4),
                "confidence": signal_data.get("confidence"),
                "error_type": post_mortem["error_type"],
                "learned_lesson": post_mortem["lesson"]
            }).execute()
        except Exception as e:
            logger.warning(f"Error inserting trade post mortem: {e}")

    # 2. Recalibrate adaptive weights for the asset
    current_params = get_adaptive_parameters(asset)
    consec_losses = current_params["consecutive_losses"]
    tot_trades = current_params["total_trades"] + 1
    
    if outcome == "won":
        consec_losses = 0
        new_win_rate = min(100.0, current_params["win_rate"] + 3.0)
        # Gradually return weights towards equilibrium
        ta_w = 0.40
        sent_w = 0.30
        macro_w = 0.30
        sl_mult = 1.5
    else:
        consec_losses += 1
        new_win_rate = max(0.0, current_params["win_rate"] - 5.0)

        ta_w = current_params["ta_weight"]
        sent_w = current_params["sentiment_weight"]
        macro_w = current_params["macro_weight"]
        sl_mult = current_params["sl_multiplier"]

        # Adapt weights depending on corrective action
        action = post_mortem["corrective_action"]
        if action == "boost_macro_weight":
            macro_w = min(0.50, macro_w + 0.05)
            ta_w = max(0.25, ta_w - 0.05)
        elif action == "expand_sl_atr":
            sl_mult = min(2.2, sl_mult + 0.2)
        elif action == "stricter_threshold":
            ta_w = min(0.50, ta_w + 0.05)
            sent_w = max(0.20, sent_w - 0.05)

    # 2bis. Apprentissage supervisé des poids (opt-in, voir ADAPTIVE_GD_ENABLED).
    # Le résultat heuristique ci-dessus sert alors de repli : si l'historique est
    # insuffisant ou la descente indisponible, le comportement nominal est intact.
    #
    # Le trade qu'on règle est **tenu hors de son propre entraînement** : sa ligne a
    # été insérée à l'étape 1, elle serait donc le premier échantillon du fit qui
    # décide des poids écrits à l'étape 3. L'adaptation s'ajusterait sur l'issue
    # qu'elle sert à expliquer, et ce trade pourrait atteindre `min_samples` à lui
    # tout seul. L'exclusion se fait par identifiant (voir
    # `_fetch_settled_post_mortems`), et **pas** sur le repli `sig_0` : ce n'est pas
    # une identité, tous les règlements sans identifiant le porteraient, donc
    # exclure sur lui retirerait les lignes d'un autre trade.
    gd_info = None
    try:
        gd_info = _gradient_descent_weights(asset, exclude_signal_id=exclude_signal_id)
    except Exception as e:  # pragma: no cover - la sûreté prime sur l'optimisation
        logger.warning(f"Descente de gradient indisponible pour {asset}, repli heuristique : {e}")
        gd_info = None

    if gd_info:
        ta_w, sent_w, macro_w = gd_info["weights"]
        held_out = gd_info["excluded_signal_id"]
        logger.info(
            "Poids appris par descente de gradient sur %s (%d trades%s) : TA=%.3f Sent=%.3f Macro=%.3f",
            asset,
            gd_info["samples"],
            f", hors échantillon : {held_out}" if held_out else "",
            ta_w,
            sent_w,
            macro_w,
        )

    # 3. Persist updated weights
    if supabase:
        try:
            supabase.table("adaptive_model_weights").upsert({
                "asset": asset,
                "ta_weight": round(ta_w, 3),
                "sentiment_weight": round(sent_w, 3),
                "macro_weight": round(macro_w, 3),
                "sl_atr_multiplier": round(sl_mult, 2),
                "tp_atr_multiplier": 3.0,
                "consecutive_losses": consec_losses,
                "win_rate_pct": round(new_win_rate, 1),
                "total_trades": tot_trades,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).execute()
        except Exception as e:
            logger.warning(f"Error updating adaptive weights: {e}")

    # 4. Refresh Knowledge Base Lessons Note
    _refresh_knowledge_base_lessons()

    return {
        "status": "learned",
        "asset": asset,
        "outcome": outcome,
        "post_mortem": post_mortem,
        "subscores": {
            "ta": round(ta_score, 4),
            "sentiment": round(sentiment_score, 4),
            "macro": round(macro_score, 4),
        },
        "weight_update": "gradient_descent" if gd_info else "heuristic",
        "gradient_descent": gd_info,
        "updated_params": {
            "ta_weight": ta_w,
            "sentiment_weight": sent_w,
            "macro_weight": macro_w,
            "consecutive_losses": consec_losses,
            "win_rate": new_win_rate
        }
    }


def _refresh_knowledge_base_lessons():
    """
    Query recent trade post-mortems and generate an updated synthesis for the knowledge base.
    """
    if not supabase:
        return

    try:
        res = supabase.table("trade_post_mortems").select("*").order("created_at", desc=True).limit(10).execute()
        rows = res.data or []
        if not rows:
            return

        lines = ["=== MÉMOIRE ET RÉTROACTION AUTO-APPRENANTE ==="]
        for r in rows:
            lines.append(f"- [{r.get('asset')}] {r.get('outcome').upper()} ({r.get('direction')}): {r.get('learned_lesson')}")

        content_str = "\n".join(lines)
        upsert_note(source=LESSONS_SOURCE, title="Système de Rétroaction et Leçons de Trading", content=content_str)
    except Exception as e:
        logger.warning(f"Error refreshing KB lessons: {e}")


def get_learning_summary() -> Dict[str, Any]:
    """
    Generate a full summary of the AI's self-learning status across all assets.
    """
    post_mortems = []
    asset_weights = []

    if supabase:
        try:
            pm_res = supabase.table("trade_post_mortems").select("*").order("created_at", desc=True).limit(20).execute()
            post_mortems = pm_res.data or []

            w_res = supabase.table("adaptive_model_weights").select("*").execute()
            asset_weights = w_res.data or []
        except Exception as e:
            logger.warning(f"Error loading learning summary: {e}")

    total_learned_lessons = len(post_mortems)
    total_losses_analyzed = sum(1 for p in post_mortems if p.get("outcome") == "lost")
    total_wins_analyzed = sum(1 for p in post_mortems if p.get("outcome") == "won")

    return {
        "status": "active",
        "total_lessons_recorded": total_learned_lessons,
        "losses_analyzed": total_losses_analyzed,
        "wins_analyzed": total_wins_analyzed,
        "asset_adaptive_profiles": asset_weights,
        "recent_post_mortems": post_mortems[:8]
    }
