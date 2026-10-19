#!/usr/bin/env python3
"""Résumé hebdomadaire Strava — exécuté par GitHub Actions chaque lundi."""
import datetime as dt
import json
import pathlib
import urllib.parse
import urllib.request
import urllib.error

BASE = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE / "config" / "strava.json"
OUT_DIR = BASE / "summaries"
DAYS = 7

TOKEN_URL = "https://www.strava.com/oauth/token"
ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"


def http_post(url: str, data: dict) -> dict:
    payload = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise SystemExit(f"ERREUR STRAVA {e.code} : {body}")


def http_get(url: str, headers: dict, params: dict) -> list:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{qs}", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise SystemExit(f"ERREUR STRAVA {e.code} : {body}")


def main() -> None:
    config = json.loads(CONFIG_PATH.read_text())

    if config.get("refresh_token"):
        resp = http_post(TOKEN_URL, {
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": config["refresh_token"],
        })
    elif config.get("auth_code"):
        resp = http_post(TOKEN_URL, {
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "grant_type": "authorization_code",
            "code": config["auth_code"],
        })
    else:
        raise SystemExit("Ni refresh_token ni auth_code dans config/strava.json")

    access_token = resp["access_token"]
    if resp.get("refresh_token"):
        config["refresh_token"] = resp["refresh_token"]
        config["auth_code"] = None
        CONFIG_PATH.write_text(json.dumps(config, indent=2))

    after = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DAYS)).timestamp())
    headers = {"Authorization": f"Bearer {access_token}"}
    activities, page = [], 1
    while True:
        batch = http_get(ACTIVITIES_URL, headers, {
            "after": after, "per_page": 100, "page": page,
        })
        if not batch:
            break
        activities.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    parsed = [
        {
            "nom": a.get("name", "Sans nom"),
            "type": a.get("type", "Unknown"),
            "km": round((a.get("distance") or 0) / 1000, 2),
            "minutes": round((a.get("moving_time") or 0) / 60),
            "denivele_m": round(a.get("total_elevation_gain") or 0, 1),
            "date": (a.get("start_date_local") or "")[:10],
        }
        for a in activities
    ]

    total_km = round(sum(p["km"] for p in parsed), 2)
    total_min = sum(p["minutes"] for p in parsed)
    total_dplus = round(sum(p["denivele_m"] for p in parsed), 1)
    par_type: dict = {}
    for p in parsed:
        t = par_type.setdefault(p["type"], {"nombre": 0, "km": 0.0})
        t["nombre"] += 1
        t["km"] = round(t["km"] + p["km"], 2)

    OUT_DIR.mkdir(exist_ok=True)
    result = {
        "genere_le": dt.datetime.now(dt.timezone.utc).isoformat(),
        "fenetre_jours": DAYS,
        "nb_activites": len(parsed),
        "distance_totale_km": total_km,
        "temps_total_min": total_min,
        "denivele_total_m": total_dplus,
        "par_type": par_type,
        "activites": parsed,
    }
    (OUT_DIR / "latest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))

    if parsed:
        h, m = divmod(total_min, 60)
        duree = f"{h}h{m:02d}" if h else f"{m} min"
        lignes = [
            f"📊 Résumé Strava des {DAYS} derniers jours",
            f"• {len(parsed)} activité(s), {total_km} km, {duree}, {total_dplus} m de D+",
        ]
        for p in parsed:
            lignes.append(f"  - {p['date']} · {p['nom']} ({p['type']}) : {p['km']} km, {p['minutes']} min, {p['denivele_m']} m D+")
    else:
        lignes = [f"Aucune activité Strava sur les {DAYS} derniers jours. Lève-toi du canapé ! 🏃"]
    (OUT_DIR / "summary.txt").write_text("\n".join(lignes))

    print("\n".join(lignes))


if __name__ == "__main__":
    main()
