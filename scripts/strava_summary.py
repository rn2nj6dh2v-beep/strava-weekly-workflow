#!/usr/bin/env python3
"""Résumé hebdomadaire Strava — exécuté par GitHub Actions le dimanche 20h (heure de Paris).

Inclut depuis le 19/10/2026 :
- Archivage hebdomadaire dans summaries/history/<année>-S<semaine ISO>.json
- Analyse IA générée via l'API Mistral (si clé présente), avec comparaison
  à la semaine précédente archivée. En cas d'échec, la raison exacte est
  journalisée dans le résumé (champ "analyse_ia_erreur").

Enrichi le 20/10/2026 :
- Données détaillées par activité : fréquence cardiaque (moy/max), vitesses
  (moy/max), calories, puissance (moy/max, vélo), cadence (moy/max — vélo ET
  course à pied), temps écoulé vs en mouvement, suffer score, effort,
  appareil d'enregistrement.
"""
import datetime as dt
import json
import os
import pathlib
import urllib.parse
import urllib.request
import urllib.error
import zoneinfo

BASE = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE / "config" / "strava.json"
MISTRAL_CONFIG_PATH = BASE / "config" / "mistral.json"
OUT_DIR = BASE / "summaries"
HISTORY_DIR = OUT_DIR / "history"
DAYS = 7
PARIS = zoneinfo.ZoneInfo("Europe/Paris")


def _heure_paris_valide() -> bool:
    """Vrai s'il est 20h (à la minute près) à Paris. Permet l'ajustement automatique
    été/hiver : GitHub déclenche à 18h et 19h UTC, on n'exécute que le bon créneau."""
    if os.environ.get("STRAVA_FORCE_RUN"):
        return True  # déclenchement manuel : toujours exécuter
    return dt.datetime.now(PARIS).hour == 20

TOKEN_URL = "https://www.strava.com/oauth/token"
ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
# Modèles essayés dans l'ordre (les suivants servent de secours)
MODELES_FALLBACK = ["mistral-small-latest", "open-mistral-nemo", "ministral-3b-latest"]


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


def _cle_mistral() -> str | None:
    """Clé API Mistral : variable d'env MISTRAL_API_KEY, sinon config/mistral.json."""
    key = os.environ.get("MISTRAL_API_KEY")
    if key:
        return key
    if MISTRAL_CONFIG_PATH.exists():
        try:
            return json.loads(MISTRAL_CONFIG_PATH.read_text()).get("api_key")
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _fichier_historique(d: dt.datetime) -> pathlib.Path:
    """Chemin d'archive pour la semaine ISO contenant la date d (ex. 2026-S42.json)."""
    annee, semaine, _ = d.isocalendar()
    return HISTORY_DIR / f"{annee}-S{semaine:02d}.json"


def _peut_etre(v, arrondi=None):
    """Retourne la valeur si elle existe et n'est pas nulle, sinon None."""
    if v is None or v == 0:
        return None
    return round(v, arrondi) if arrondi is not None else v


def _parse_activite(a: dict) -> dict:
    """Extrait l'ensemble des données utiles d'une activité Strava.
    Les champs absents selon le matériel (ex. puissance sans capteur) restent None."""
    return {
        "nom": a.get("name", "Sans nom"),
        "type": a.get("type", "Unknown"),
        "date": (a.get("start_date_local") or "")[:10],
        "km": round((a.get("distance") or 0) / 1000, 2),
        "minutes": round((a.get("moving_time") or 0) / 60),
        "minutes_ecoulees": round((a.get("elapsed_time") or 0) / 60),
        "denivele_m": round(a.get("total_elevation_gain") or 0, 1),
        # Fréquence cardiaque (si mesurée par l'appareil)
        "fc_moyenne": _peut_etre(a.get("average_heartrate")),
        "fc_max": _peut_etre(a.get("max_heartrate")),
        # Vitesses (m/s -> km/h pour la lisibilité)
        "vitesse_moyenne_kmh": _peut_etre((a.get("average_speed") or 0) * 3.6, 1),
        "vitesse_max_kmh": _peut_etre((a.get("max_speed") or 0) * 3.6, 1),
        # Énergie et effort
        "calories": _peut_etre(a.get("calories")),
        "suffer_score": a.get("suffer_score") or None,
        "effort": a.get("effort") or None,
        # Puissance et cadence de pédalage (vélo, si capteurs présents)
        "puissance_moyenne_w": _peut_etre(a.get("average_watts")),
        "puissance_max_w": _peut_etre(a.get("max_watts")),
        "cadence_moyenne": _peut_etre(a.get("average_cadence"), 1),
        "cadence_max": _peut_etre(a.get("max_cadence"), 1),
        # Appareil d'enregistrement
        "appareil": a.get("device_name") or None,
    }


def _resume_court(data: dict) -> str:
    lignes = [
        f"{data['nb_activites']} activités, {data['distance_totale_km']} km, "
        f"{data['temps_total_min']} min, {data['denivele_total_m']} m D+"
    ]
    for t, v in data.get("par_type", {}).items():
        lignes.append(f"  {t}: {v['nombre']} sorties, {v['km']} km")
    for a in data.get("activites", []):
        detail = f"  {a['date']} {a['type']} \"{a['nom']}\": {a['km']} km, {a['minutes']} min, {a['denivele_m']} m D+"
        extras = []
        if a.get("fc_moyenne"):
            extras.append(f"FC {a['fc_moyenne']}/{a.get('fc_max')} bpm")
        if a.get("vitesse_moyenne_kmh"):
            extras.append(f"{a['vitesse_moyenne_kmh']} km/h moy (max {a.get('vitesse_max_kmh')})")
        if a.get("calories"):
            extras.append(f"{a['calories']} kcal")
        if a.get("puissance_moyenne_w"):
            extras.append(f"{a['puissance_moyenne_w']} W moy")
        if a.get("cadence_moyenne"):
            extras.append(f"cadence {a['cadence_moyenne']}")
        if a.get("suffer_score"):
            extras.append(f"souffrance {a['suffer_score']}")
        if extras:
            detail += " [" + ", ".join(str(e) for e in extras) + "]"
        lignes.append(detail)
    return "\n".join(lignes)


def _analyse_ia(data: dict, precedent: dict | None) -> tuple[str | None, str | None]:
    """Appelle l'API Mistral pour une analyse d'entraînement en français.
    Retourne (analyse, None) en cas de succès, ou (None, raison) en cas d'échec.
    N'interrompt jamais le résumé."""
    key = _cle_mistral()
    if not key:
        return None, "clé MISTRAL_API_KEY absente (ni variable d'env ni config/mistral.json)"
    prompt = (
        "Tu es un coach sportif. Analyse la semaine d'entraînement Strava ci-dessous. "
        "Exploite les données de fréquence cardiaque, vitesse, puissance et cadence quand "
        "elles sont présentes pour évaluer l'intensité réelle des efforts. "
        "Réponds en français, en 150 mots maximum : 2-3 points forts, 1-2 points de "
        "vigilance, 1 conseil concret pour la semaine suivante. Ton direct et encourageant, "
        "pas de listes à puces, du texte fluide en 2 paragraphes courts.\n\n"
        "SEMAINE À ANALYSER :\n" + _resume_court(data)
    )
    if precedent:
        prompt += "\n\nSEMAINE PRÉCÉDENTE (pour comparaison) :\n" + _resume_court(precedent)

    derniere_erreur = None
    for modele in MODELES_FALLBACK:
        payload = json.dumps({
            "model": modele,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 400,
            "temperature": 0.5,
        }).encode()
        req = urllib.request.Request(MISTRAL_CHAT_URL, data=payload, method="POST", headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode())
            return body["choices"][0]["message"]["content"].strip(), None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            derniere_erreur = f"HTTP {e.code} (modèle {modele}) : {detail}"
        except (urllib.error.URLError, KeyError, IndexError) as e:
            derniere_erreur = f"{type(e).__name__} (modèle {modele}) : {e}"
    return None, derniere_erreur or "erreur inconnue"


def main() -> None:
    if not _heure_paris_valide():
        print("Pas encore 20h à Paris — rien à faire (ajustement heure été/hiver).")
        return

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

    parsed = [_parse_activite(a) for a in activities]

    total_km = round(sum(p["km"] for p in parsed), 2)
    total_min = sum(p["minutes"] for p in parsed)
    total_dplus = round(sum(p["denivele_m"] for p in parsed), 1)
    total_kcal = sum(p["calories"] for p in parsed if p["calories"]) or None
    par_type: dict = {}
    for p in parsed:
        t = par_type.setdefault(p["type"], {"nombre": 0, "km": 0.0})
        t["nombre"] += 1
        t["km"] = round(t["km"] + p["km"], 2)

    OUT_DIR.mkdir(exist_ok=True)
    HISTORY_DIR.mkdir(exist_ok=True)
    maintenant = dt.datetime.now(dt.timezone.utc)

    result = {
        "genere_le": maintenant.isoformat(),
        "fenetre_jours": DAYS,
        "nb_activites": len(parsed),
        "distance_totale_km": total_km,
        "temps_total_min": total_min,
        "denivele_total_m": total_dplus,
        "calories_totales": total_kcal,
        "par_type": par_type,
        "activites": parsed,
    }

    # Semaine précédente archivée (pour comparaison IA), si elle existe
    precedent = None
    fichier_prec = _fichier_historique(maintenant - dt.timedelta(weeks=1))
    if fichier_prec.exists():
        try:
            precedent = json.loads(fichier_prec.read_text())
        except json.JSONDecodeError:
            precedent = None

    # Analyse IA (best effort : n'interrompt jamais le résumé)
    analyse, raison_erreur = _analyse_ia(result, precedent)
    if analyse:
        result["analyse_ia"] = analyse
        result["modele_ia"] = MODELES_FALLBACK[0]
    elif raison_erreur:
        result["analyse_ia_erreur"] = raison_erreur

    # Archivage de la semaine + fichier "latest"
    (OUT_DIR / "latest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    archive = _fichier_historique(maintenant)
    archive.write_text(json.dumps(result, ensure_ascii=False, indent=2))

    if parsed:
        h, m = divmod(total_min, 60)
        duree = f"{h}h{m:02d}" if h else f"{m} min"
        entete = [
            f"📊 Résumé Strava des {DAYS} derniers jours",
            f"• {len(parsed)} activité(s), {total_km} km, {duree}, {total_dplus} m de D+",
        ]
        if total_kcal:
            entete.append(f"• {total_kcal} kcal brûlées")
        lignes = list(entete)
        for p in parsed:
            ligne = f"  - {p['date']} · {p['nom']} ({p['type']}) : {p['km']} km, {p['minutes']} min, {p['denivele_m']} m D+"
            extras = []
            if p.get("fc_moyenne"):
                extras.append(f"FC {p['fc_moyenne']}/{p.get('fc_max')} bpm")
            if p.get("vitesse_moyenne_kmh"):
                extras.append(f"{p['vitesse_moyenne_kmh']} km/h moy (max {p.get('vitesse_max_kmh')})")
            if p.get("calories"):
                extras.append(f"{p['calories']} kcal")
            if p.get("puissance_moyenne_w"):
                extras.append(f"{p['puissance_moyenne_w']} W moy")
            if p.get("cadence_moyenne"):
                extras.append(f"cadence {p['cadence_moyenne']}")
            if p.get("suffer_score"):
                extras.append(f"souffrance {p['suffer_score']}")
            if p.get("appareil"):
                extras.append(str(p["appareil"]))
            if extras:
                ligne += " [" + ", ".join(str(e) for e in extras) + "]"
            lignes.append(ligne)
        if analyse:
            lignes += ["", "🧠 Analyse IA :", analyse]
        else:
            lignes += ["", f"🧠 Analyse IA : indisponible — raison : {raison_erreur}"]
    else:
        lignes = [f"Aucune activité Strava sur les {DAYS} derniers jours. Lève-toi du canapé ! 🏃"]
    (OUT_DIR / "summary.txt").write_text("\n".join(lignes))

    print("\n".join(lignes))


if __name__ == "__main__":
    main()
