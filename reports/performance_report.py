from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from database.supabase_client import supabase

ARTIFACT_DIR = Path(__file__).resolve().parents[1] / "artifacts"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _fetch_signals(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    query = supabase.table("pending_signals").select("*").order("validated_at", desc=True)
    if user_id:
        query = query.eq("user_id", user_id)
    res = query.execute()
    return res.data or []


def _trade_return_pct(row: Dict[str, Any]) -> Optional[float]:
    signal = row.get("signal") or {}
    result = row.get("execution_result") or {}
    entry = _safe_float(signal.get("entry"))
    closed = _safe_float(result.get("closed_price"))
    direction = str(signal.get("direction") or "").upper()
    status = str(row.get("status") or "").lower()
    if entry <= 0 or closed <= 0 or direction not in {"BUY", "SELL"}:
        return None
    if direction == "BUY":
        raw = (closed - entry) / entry * 100
    else:
        raw = (entry - closed) / entry * 100
    if status == "lost" and raw > 0:
        raw = -raw
    if status == "won" and raw < 0:
        raw = abs(raw)
    return round(raw, 3)


def build_performance_summary(user_id: Optional[str] = None) -> Dict[str, Any]:
    rows = _fetch_signals(user_id)
    won = [r for r in rows if r.get("status") == "won"]
    lost = [r for r in rows if r.get("status") == "lost"]
    executed = [r for r in rows if r.get("status") == "executed"]
    pending = [r for r in rows if r.get("status") == "pending"]
    settled = won + lost

    returns = [x for x in (_trade_return_pct(r) for r in settled) if x is not None]
    avg_return = round(sum(returns) / len(returns), 3) if returns else 0.0

    by_asset: Dict[str, Dict[str, Any]] = {}
    for row in settled:
        signal = row.get("signal") or {}
        asset = str(signal.get("asset") or "?")
        bucket = by_asset.setdefault(asset, {"asset": asset, "won": 0, "lost": 0, "count": 0})
        bucket["count"] += 1
        if row.get("status") == "won":
            bucket["won"] += 1
        else:
            bucket["lost"] += 1

    assets_ranked = sorted(
        [
            {
                **v,
                "win_rate": round((v["won"] / v["count"] * 100) if v["count"] else 0.0, 1),
            }
            for v in by_asset.values()
        ],
        key=lambda x: (-x["count"], -x["win_rate"], x["asset"]),
    )

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "user" if user_id else "global",
        "user_id": user_id,
        "counts": {
            "won": len(won),
            "lost": len(lost),
            "executed": len(executed),
            "pending": len(pending),
            "settled": len(settled),
            "all": len(rows),
        },
        "win_rate": round((len(won) / len(settled) * 100) if settled else 0.0, 1),
        "avg_settled_return_pct": avg_return,
        "best_assets": assets_ranked[:5],
        "recent": [
            {
                "id": r.get("id"),
                "status": r.get("status"),
                "asset": (r.get("signal") or {}).get("asset"),
                "direction": (r.get("signal") or {}).get("direction"),
                "validated_at": r.get("validated_at"),
                "return_pct": _trade_return_pct(r),
            }
            for r in rows[:10]
        ],
    }
    return summary


def to_markdown(summary: Dict[str, Any]) -> str:
    counts = summary.get("counts") or {}
    lines = [
        f"# Run Card — {'Utilisateur' if summary.get('scope') == 'user' else 'Global'}",
        "",
        f"- Généré le : {summary.get('generated_at')}",
        f"- Trades réglés : {counts.get('settled', 0)}",
        f"- Gagnés : {counts.get('won', 0)}",
        f"- Perdus : {counts.get('lost', 0)}",
        f"- En cours : {counts.get('executed', 0)}",
        f"- En attente : {counts.get('pending', 0)}",
        f"- Win rate : {summary.get('win_rate', 0)}%",
        f"- Rendement moyen réglé : {summary.get('avg_settled_return_pct', 0)}%",
        "",
        "## Top actifs",
    ]
    top_assets = summary.get("best_assets") or []
    if not top_assets:
        lines.append("- Aucun trade réglé pour l'instant")
    else:
        for asset in top_assets:
            lines.append(
                f"- {asset['asset']}: {asset['count']} trades, win rate {asset['win_rate']}%"
            )
    lines.extend(["", "## Trades récents"])
    recents = summary.get("recent") or []
    if not recents:
        lines.append("- Aucun historique")
    else:
        for item in recents:
            ret = item.get("return_pct")
            suffix = f" | retour {ret}%" if ret is not None else ""
            lines.append(
                f"- {item.get('asset')} {item.get('direction')} — {item.get('status')} — {item.get('validated_at')}{suffix}"
            )
    return "\n".join(lines)


def export_run_card(user_id: Optional[str] = None) -> Dict[str, Any]:
    """Génère la run card et la renvoie AUSSI en clair dans la réponse.

    Le disque de Render est éphémère : les fichiers écrits dans `artifacts/`
    disparaissent à chaque redéploiement. On renvoie donc également le
    contenu (markdown + résumé) pour que l'appelant puisse l'envoyer/persister
    sans dépendre du système de fichiers.
    """
    summary = build_performance_summary(user_id)
    markdown = to_markdown(summary)
    scope = user_id or "global"

    json_file = md_file = None
    try:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        json_path = ARTIFACT_DIR / f"run_card_{scope}.json"
        md_path = ARTIFACT_DIR / f"run_card_{scope}.md"
        json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path.write_text(markdown, encoding="utf-8")
        json_file, md_file = json_path.name, md_path.name
    except Exception as e:
        # Disque non inscriptible (FS éphémère/lecture seule) : on continue,
        # le contenu reste disponible dans la réponse.
        print(f"⚠️ export_run_card écriture disque ignorée: {type(e).__name__}: {e}")

    return {
        "scope": scope,
        "json_file": json_file,
        "markdown_file": md_file,
        "markdown": markdown,
        "summary": summary,
    }
