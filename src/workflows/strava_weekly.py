from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from pydantic import BaseModel, Field

import mistralai.workflows as workflows

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"


class WeeklySummaryInput(BaseModel):
    days: int = Field(default=7, ge=1, le=90, description="Fenêtre en jours d'activités à résumer")
    client_id: str = Field(description="Client ID de l'application Strava")
    client_secret: str = Field(description="Client Secret de l'application Strava")
    refresh_token: str | None = Field(default=None, description="Refresh token Strava permanent")
    auth_code: str | None = Field(default=None, description="Code d'autorisation à usage unique (première exécution)")
    mistral_api_key: str | None = Field(default=None, description="Clé API Mistral pour le résumé IA (optionnel)")


class ActivitySummary(BaseModel):
    name: str
    type: str
    distance_km: float
    moving_time_min: int
    total_elevation_m: float
    start_date: str


class WorkflowOutput(BaseModel):
    summary: str
    new_refresh_token: str | None = None


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
async def get_access_token(client_id: str, client_secret: str, refresh_token: str | None, auth_code: str | None) -> dict[str, str]:
    """Obtient un access token Strava : via refresh token si présent, sinon échange le code d'autorisation."""
    if refresh_token:
        resp = await _post(STRAVA_TOKEN_URL, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })
    elif auth_code:
        resp = await _post(STRAVA_TOKEN_URL, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": auth_code,
        })
    else:
        raise ValueError("Fournir refresh_token ou auth_code en entrée")

    if resp.get("access_token") is None:
        raise RuntimeError(f"Échec de l'obtention du token Strava : {resp}")

    return {
        "access_token": resp["access_token"],
        "refresh_token": resp.get("refresh_token") or refresh_token or "",
    }


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


def _format_duration(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


@workflows.activity()
async def build_summary(activities: list[dict[str, Any]], days: int, mistral_api_key: str | None) -> str:
    """Calcule les statistiques et rédige le résumé (IA si clé dispo, sinon modèle fixe)."""
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

    total_km = round(sum(p.distance_km for p in parsed), 2)
    total_min = sum(p.moving_time_min for p in parsed)
    total_dplus = round(sum(p.total_elevation_m for p in parsed), 1)

    fallback = (
        f"📊 Résumé des {days} derniers jours : {len(parsed)} activités, "
        f"{total_km} km, {_format_duration(total_min)} d'effort, {total_dplus} m de D+. "
        "Bonne semaine, continue comme ça ! 💪"
    )

    api_key = mistral_api_key
    if not api_key:
        try:
            import os
            api_key = os.environ.get("MISTRAL_API_KEY")
        except Exception:
            api_key = None
    if not api_key:
        return fallback

    stats = {
        "nb_activites": len(parsed),
        "distance_totale_km": total_km,
        "temps_total_min": total_min,
        "denivele_total_m": total_dplus,
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
    from mistralai import Mistral

    client = Mistral(api_key=api_key)
    prompt = (
        "Voici mes statistiques sportives des "
        f"{days} derniers jours (JSON) :\n{json.dumps(stats, ensure_ascii=False)}\n\n"
        "Rédige un résumé motivant et concis (5-8 phrases) en français : "
        "volume, répartition par sport, point fort, et un encouragement "
        "pour la semaine prochaine. Termine par les 3 chiffres clés sur une ligne."
    )
    chat = await client.chat.async_create(
        model="mistral-small-latest",
        messages=[{"role": "user", "content": prompt}],
    )
    return chat.choices[0].message.content or fallback


@workflows.workflow.define(
    name="strava-weekly-summary",
    workflow_display_name="Résumé hebdomadaire Strava",
    workflow_description="Récupère les activités Strava et rédige un résumé d'entraînement.",
)
class StravaWeeklyWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: WeeklySummaryInput) -> WorkflowOutput:
        tokens = await get_access_token(
            input.client_id, input.client_secret, input.refresh_token, input.auth_code,
        )
        activities = await fetch_activities(tokens["access_token"], input.days)
        summary = await build_summary(activities, input.days, input.mistral_api_key)

        new_refresh = None
        if input.auth_code and tokens.get("refresh_token"):
            new_refresh = tokens["refresh_token"]

        return WorkflowOutput(summary=summary, new_refresh_token=new_refresh)
