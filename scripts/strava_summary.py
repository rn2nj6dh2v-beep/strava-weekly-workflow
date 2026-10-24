#!/usr/bin/env python3
"""Résumé hebdomadaire Strava — exécuté par GitHub Actions le dimanche 23h (heure de Paris).

- Archivage hebdomadaire dans summaries/history/<année>-S<semaine ISO>.json
- Analyse IA via l'API Mistral (comparaison avec la semaine précédente archivée)
- Données enrichies par activité : FC, vitesses, calories, puissance, cadence,
  suffer score, appareil
- Envoi du résumé par email (SMTP) si SMTP_USER/SMTP_PASSWORD/MAIL_TO présents
"""
import datetime as dt
import json
import os
import pathlib
import smtplib
import ssl
import urllib.parse
import urllib.request
import urllib.error
from email.message import EmailMessage
import zoneinfo

BASE = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE / "config" / "strava.json"
MISTRAL_CONFIG_PATH = BASE / "config" / "mistral.json"
OUT_DIR = BASE / "summaries"
HISTORY_DIR = OUT_DIR / "history"
DAYS = 7
PARIS = zoneinfo.ZoneInfo("Europe/Paris")
HEURE_EXECUTION = 23  # heure de Paris
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))


def _heure_paris_valide() -> bool:
    """Vrai s'il est HEURE_EXECUTION à Paris. Permet l'ajustement automatique
    été/hiver : GitHub déclenche aux deux créneaux UTC possibles, on n'exécute
    que le bon."""
    if os.environ.get("STRAVA_FORCE_RUN"):
        return True  # déclenchement manuel : toujours exécuter
    return dt.datetime.now(PARIS).hour == HEURE_EXECUTION

TOKEN_URL = "https://www.strava.com/oauth/token"
ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
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
    key = os.environ.get("MISTRAL_API_KEY")
    if key:
        return key
    if MISTRAL_CONFIG_PATH.exists():
        try:
            return json.loads(MISTRAL_CONFIG_PATH.read_text()).get("api_key")
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _envoi_email(contenu: str) -> str | None:
    """Envoie le résumé par email via SMTP (Gmail par défaut).
    Retourne None si succès, ou la raison de l'échec. Best effort."""
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    to = os.environ.get("MAIL_TO") or user
    if not user or not password:
        return "secrets SMTP_USER / SMTP_PASSWORD absents — email non envoyé"
    sujet = f"📊 Ton résumé Strava de la semaine — {dt.datetime.now(PARIS).strftime('%d/%m/%Y')}"
    msg = EmailMessage()
    msg["Subject"] = sujet
    msg["From"] = f"Strava Bot <{user}>"
    msg["To"] = to
    msg.set_content(contenu)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(user, password)
            server.send_message(msg)
        return None
    except Exception as e:
        return f"{type(e).__name__} : {e}"


def _fichier_historique(d: dt.datetime) -> pathlib.Path:
    annee, semaine, _ = d.isocalendar()
    return HISTORY_DIR / f"{annee}-S{semaine:02d}.json"


def _peut_etre(v, arrondi=None):
    if v is None or v == 0:
        return None
    return round(v, arrondi) if arrondi is not None else v


def _parse_activite(a: dict) -> dict:
    return {
        "nom": a.get("name", "Sans nom"),
        "type": a.get("type", "Unknown"),
        "date": (a.get("start_date_local") or "")[:10],
        "km": round((a.get("distance") or 0) / 1000, 2),
        "minutes": round((a.get("moving_time") or 0) / 60),
        "minutes_ecoulees": round((a.get("elapsed_time") or 0) / 60),
        "denivele_m": round(a.get("total_elevation_gain") or 0, 1),
        "fc_moyenne": _peut_etre(a.get("average_heartrate")),
        "fc_max": _peut_etre(a.get("max_heartrate")),
        "vitesse_moyenne_kmh": _peut_etre((a.get("average_speed") or 0) * 3.6, 1),
        "vitesse_max_kmh": _peut_etre((a.get("max_speed") or 0) * 3.6, 1),
        "calories": _peut_etre(a.get("calories")),
        "suffer_score": a.get("suffer_score") or None,
        "effort": a.get("effort") or None,
        "puissance_moyenne_w": _peut_etre(a.get("average_watts")),
        "puissance_max_w": _peut_etre(a.get("max_watts")),
        "cadence_moyenne": _peut_etre(a.get("average_cadence"), 1),
        "cadence_max": _peut_etre(a.get("max_cadence"), 1),
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
        print(f"Pas encore {HEURE_EXECUTION}h à Paris — rien à faire (ajustement heure été/hiver).")
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

    precedent = None
    fichier_prec = _fichier_historique(maintenant - dt.timedelta(weeks=1))
    if fichier_prec.exists():
        try:
            precedent = json.loads(fichier_prec.read_text())
        except json.JSONDecodeError:
            precedent = None

    analyse, raison_erreur = _analyse_ia(result, precedent)
    if analyse:
        result["analyse_ia"] = analyse
        result["modele_ia"] = MODELES_FALLBACK[0]
    elif raison_erreur:
        result["analyse_ia_erreur"] = raison_erreur

    (OUT_DIR / "latest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    archive = _fichier_historique(maintenant)
    archive.write_text(json.dumps(result, ensure_ascii=False, indent=2))

    if parsed:
        h, m = divmod(total_min, 60)
        duree = f"{h}h{m:02d}" if h else f"{m} min"
        lignes = [
            f"📊 Résumé Strava des {DAYS} derniers jours",
            f"• {len(parsed)} activité(s), {total_km} km, {duree}, {total_dplus} m de D+",
        ]
        if total_kcal:
            lignes.append(f"• {total_kcal} kcal brûlées")
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

    # Envoi email (best effort : n'interrompt jamais le résumé)
    erreur_email = _envoi_email("\n".join(lignes))
    if erreur_email:
        print(f"Email non envoyé — raison : {erreur_email}")
        result["email_erreur"] = erreur_email
        (OUT_DIR / "latest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Résumé envoyé par email ✉️")

    print("\n".join(lignes))


if __name__ == "__main__":
    main()
