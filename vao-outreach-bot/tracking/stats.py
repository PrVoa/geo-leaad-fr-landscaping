"""Calcul des KPIs de campagne."""

from __future__ import annotations

import datetime
import logging

from rich.console import Console
from rich.table import Table

from db.client import get_client

log = logging.getLogger("vao.stats")


def get_pipeline_stats() -> list[dict]:
    """Stats pipeline par statut et tier."""
    return (
        get_client()
        .table("landscapers")
        .select("campaign_status, tier, id")
        .not_.is_("campaign_status", "null")
        .execute()
        .data
    )


def get_submission_stats(days: int = 30) -> dict:
    """Stats des soumissions sur les N derniers jours."""
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    rows = (
        get_client()
        .table("submissions")
        .select("status, channel, sequence_step, message_variant")
        .gte("attempted_at", since)
        .execute()
        .data
    )

    stats = {
        "total": len(rows),
        "success": sum(1 for r in rows if r["status"] == "success"),
        "failed": sum(1 for r in rows if r["status"].startswith("failed")),
        "by_channel": {},
        "by_step": {},
        "by_variant": {},
    }

    for r in rows:
        ch = r.get("channel", "?")
        stats["by_channel"].setdefault(ch, {"total": 0, "success": 0})
        stats["by_channel"][ch]["total"] += 1
        if r["status"] == "success":
            stats["by_channel"][ch]["success"] += 1

        step = r.get("sequence_step", 0)
        stats["by_step"].setdefault(step, {"total": 0, "success": 0})
        stats["by_step"][step]["total"] += 1
        if r["status"] == "success":
            stats["by_step"][step]["success"] += 1

        var = r.get("message_variant", "?")
        stats["by_variant"].setdefault(var, {"total": 0, "success": 0})
        stats["by_variant"][var]["total"] += 1
        if r["status"] == "success":
            stats["by_variant"][var]["success"] += 1

    return stats


def get_response_stats() -> dict:
    """Stats des réponses reçues."""
    rows = (
        get_client()
        .table("responses")
        .select("sentiment, intent")
        .execute()
        .data
    )
    stats = {
        "total": len(rows),
        "by_sentiment": {},
        "by_intent": {},
    }
    for r in rows:
        s = r.get("sentiment", "?")
        stats["by_sentiment"][s] = stats["by_sentiment"].get(s, 0) + 1
        i = r.get("intent", "?")
        stats["by_intent"][i] = stats["by_intent"].get(i, 0) + 1
    return stats


def print_dashboard() -> None:
    """Affiche le dashboard complet dans le terminal."""
    console = Console()

    # Pipeline
    pipeline_data = get_pipeline_stats()
    pipeline: dict[str, dict] = {}
    for row in pipeline_data:
        status = row.get("campaign_status", "?")
        tier = row.get("tier")
        key = f"{status} (T{tier})" if tier else status
        pipeline[key] = pipeline.get(key, 0)
        pipeline[key] = pipeline.get(key, 0) + 1

    table = Table(title="Pipeline")
    table.add_column("Statut", min_width=25)
    table.add_column("Count", justify="right")
    for status, count in sorted(pipeline.items()):
        table.add_row(status, str(count))
    console.print(table)

    # Submissions
    sub_stats = get_submission_stats()
    console.print(f"\n[bold]Soumissions (30j)[/bold] — {sub_stats['total']} total, "
                  f"{sub_stats['success']} succès "
                  f"({sub_stats['success'] / max(sub_stats['total'], 1) * 100:.0f}%)")

    if sub_stats["by_step"]:
        step_table = Table(title="Par step")
        step_table.add_column("Step")
        step_table.add_column("Total", justify="right")
        step_table.add_column("Succès", justify="right")
        step_table.add_column("Taux", justify="right")
        for step in sorted(sub_stats["by_step"]):
            s = sub_stats["by_step"][step]
            rate = s["success"] / max(s["total"], 1) * 100
            step_table.add_row(str(step), str(s["total"]), str(s["success"]), f"{rate:.0f}%")
        console.print(step_table)

    if sub_stats["by_variant"]:
        var_table = Table(title="Par variant")
        var_table.add_column("Variant")
        var_table.add_column("Total", justify="right")
        var_table.add_column("Succès", justify="right")
        var_table.add_column("Taux", justify="right")
        for var in sorted(sub_stats["by_variant"]):
            s = sub_stats["by_variant"][var]
            rate = s["success"] / max(s["total"], 1) * 100
            var_table.add_row(var, str(s["total"]), str(s["success"]), f"{rate:.0f}%")
        console.print(var_table)

    # Responses
    resp_stats = get_response_stats()
    if resp_stats["total"]:
        console.print(f"\n[bold]Réponses[/bold] — {resp_stats['total']} total")
        for sentiment, count in resp_stats["by_sentiment"].items():
            console.print(f"  {sentiment}: {count}")
