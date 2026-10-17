from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from pydantic import BaseModel, Field

import mistralai.workflows as workflows

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"


class WeeklySummaryInput(BaseModel):
    days: int = Field(default=7, ge=1, le=90, description="Fenêtre en jours d'activités à résumer")


class ActivitySummary(BaseModel):
    name: str
    type: str
    distance_km: float
    moving_time_min: int
    total_elevation_m: float
    start_date: str


async def _post(url: str, data: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, data=data)
        r.raise_for_status()
        return r.json()


async def _get(url: str, headers: dict[str, str], params: dict[str, Any]) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers, params=params)
        r.raise_for_status()
        return r.json()


@workflows.activity()
async def get_access_token() -> str:
    """Rafraîchit le token d'accès Strava (les tokens expirent toutes les 6 h)."""
    import os

    resp = await _post(STRAVA_TOKEN_URL, data={
        "client_id": os.environ["STRAVA_CLIENT_ID"],
        "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
        "grant_type": "refresh_token",
        "refresh_token": os.environ["STRAVA_REFRESH_TOKEN"],
    })
    if resp.get("access_token") is None:
        raise RuntimeError(f"Échec du rafraîchissement du token Strava : {resp}")
    return resp["access_token"]


@workflows.activity()
async def fetch_activities(access_token: str, days: int) -> list[dict[str, Any]]:
    """Récupère les activités Strava des N derniers jours."""
    after = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    headers = {"Authorization": f"Bearer {access_token}"}
    activities: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = await _get(STRAVA_ACTIVITIES_URL, headers=headers, params={
            "after": after, "per_page": 100, "page": page,
        })
        if not batch:
            break
        activities.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return activities


@workflows.activity()
async def build_summary(activities: list[dict[str, Any]], days: int) -> str:
    """Calcule les statistiques et rédige le résumé avec Mistral AI."""
    parsed = [
        ActivitySummary(
            name=a.get("name", "Sans nom"),
            type=a.get("type", "Unknown"),
            distance_km=round((a.get("distance") or 0) / 1000, 2),
            moving_time_min=round((a.get("moving_time") or 0) / 60),
            total_elevation_m=round(a.get("total_elevation_gain") or 0, 1),
            start_date=(a.get("start_date_local") or "")[:10],
        )
        for a in activities
    ]

    if not parsed:
        return (
            f"Aucune activité Strava sur les {days} derniers jours. "
            "Lève-toi du canapé ! 🏃"
        )

    stats = {
        "nb_activites": len(parsed),
        "distance_totale_km": round(sum(p.distance_km for p in parsed), 2),
        "temps_total_min": sum(p.moving_time_min for p in parsed),
        "denivele_total_m": round(sum(p.total_elevation_m for p in parsed), 1),
        "par_type": {},
        "activites": [p.model_dump() for p in parsed],
    }
    types: dict[str, list[float]] = {}
    for p in parsed:
        t = types.setdefault(p.type, [0, 0.0])
        t[0] += 1
        t[1] += p.distance_km
    stats["par_type"] = {k: {"nombre": v[0], "km": round(v[1], 2)} for k, v in types.items()}

    import json
    import os
    from mistralai import Mistral

    client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
    prompt = (
        "Voici mes statistiques sportives des "
        f"{days} derniers jours (JSON) :
{json.dumps(stats, ensure_ascii=False)}

"
        "Rédige un résumé motivant et concis (5-8 phrases) en français : "
        "volume, répartition par sport, point fort, et un encouragement "
        "pour la semaine prochaine. Termine par les 3 chiffres clés sur une ligne."
    )
    chat = await client.chat.async_create(
        model="mistral-small-latest",
        messages=[{"role": "user", "content": prompt}],
    )
    return chat.choices[0].message.content or ""


@workflows.workflow.define(
    name="strava-weekly-summary",
    workflow_display_name="Résumé hebdomadaire Strava",
    workflow_description="Récupère les activités Strava et rédige un résumé d'entraînement.",
)
class StravaWeeklyWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: WeeklySummaryInput) -> str:
        token = await get_access_token()
        activities = await fetch_activities(token, input.days)
        return await build_summary(activities, input.days)
