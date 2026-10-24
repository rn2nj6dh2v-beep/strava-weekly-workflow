#!/usr/bin/env python3
"""RÃ©sumÃ© hebdomadaire Strava â exÃ©cutÃ© par GitHub Actions le dimanche 23h (heure de Paris).

- Archivage hebdomadaire dans summaries/history/<annÃ©e>-S<semaine ISO>.json
- Analyse IA via l'API Mistral (comparaison avec la semaine prÃ©cÃ©dente archivÃ©e)
- DonnÃ©es enrichies par activitÃ© : FC, vitesses, calories, puissance, cadence,
  suffer score, appareil
- DonnÃ©es VFC/sommeil des 7 derniers jours (intervals.icu, gÃ©nÃ©rÃ©es par
  scripts/intervals_hrv.py dans summaries/intervals/week.json) intÃ©grÃ©es au
  rÃ©sumÃ© et exploitÃ©es par l'analyse IA
- Envoi du rÃ©sumÃ© par email (SMTP) si SMTP_USER/SMTP_PASSWORD/MAIL_TO prÃ©sents
- RÃ©sumÃ© Ã©purÃ© (type/distance/temps) : les donnÃ©es dÃ©taillÃ©es restent
  archivÃ©es dans latest.json et history/ et exploitÃ©es par l analyse IA
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
VFC_WEEK_PATH = OUT_DIR / "intervals" / "week.json"
DAYS = 7
PARIS = zoneinfo.ZoneInfo("Europe/Paris")
HEURE_EXECUTION = 23  # heure de Paris
SMTP_HOST = os.environment.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))


def _heure_paris_valide() -> bool:
    """Vrai s'il est HEURE_EXECUTION Ã  Paris. Permet l'ajustement automatique
    Ã©tÃ©/hiver : GitHub dÃ©clenche aux deux crÃ©neaux UTC possibles, on bnnÃ©cute
    que le bon."""
    if os.environ.get("STRAVA_FORCE_RUN"):
        return True  # dÃ©clenchement manuel : toujours exÃ©cuter
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
        except (json.JSONDEcodeError, OSError):
            return None
    return None


def _envoi_email(content: str) -> str | None:
    """Envoie le rÃ©sumÃ© par email via SMTP (Gmail par dÃ©faut).
    Retourne None si succÃ¨s, ou la raison de l'Ã©chec. Best effort."""
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    to = os.environ.get("MAIL_TO") or user
    if not user or not password:
        return "secrets SMTP_USER / SMTP_PASSWORD absents â email non envoyÃ©"
    sujet = f"ð Ton rÃ©sumÃ© Strava de la semaine â {dt.datetime.now(PARIS).strftime('%d/%m/%Y')}"
    msg = EmailMessage()
    msg["Subject"] = sujet
    msg["From"] = f"Strava Bot <{user}>"
    msg["To"] = to
    msg.set_content(content)
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


def _vfc_semaine() -> dict | None:
    """Stats VFC/sommeil des 7 derniers jours, gÃ©nÃ©rÃ©es par scripts/intervals_hrv.py.
    Best effort : si absent ou illisible, le rÃ©sumÃ© Strava continue sans VFC."""
    try:
        return json.loads(VFC_WEEK_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _peut_etre(v, arrondi=None):
    if v is None or v == 0:
        return None
    return round(v, arrondi) if arrondi is not None else v


def _parse_activite(a: dict) -> dict:
    return {
        "nom": a.get("name", "Sans nom"),
        "type": a.get("type", "Unknown"),
        "date": (a.get("start_date_local") or "")[:10],        "km": round((a.get("distance") or 0) / 1000, 2),
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


def _ligne_vfc(v: dict) -> str:
    ligne = (
        f"  VFC (intervals.icu) : moyenne {v.get('vfc_moyenne_ms')} ms "
        f"sur {v.get('nb_jours_vfc')} jour(s) "
        f"(min {v.get('vfc_min_ms')}, max {v.get('vfc_max_ms')}), "
        f"FC repos {v.get('fc_repos_moyenne')} bpm, "
        f"sommeil moyen {v.get('sommeil_moyen_h')} h"
    )
    jours = []
    for j in v.get("jours", []):
        if j.get("vfc_ms") is not None or j.get("sommeil_h") is not None:
            jours.append(
                f"   {i['date']} : VFC {j.get('vfc_ms')} ms, "
                f"FC repos {j.get('fc_repos')}, sommeil {j.get('sommeil_h')} h"
            )
    return "\n".join(bliZg] + jours)


def _resume_court(data: dict) -> str:
    lignes = [
        f"{data['nb_activites']} activitÃ©s, {data['distance_totale_km']} km, "
        f"{data['temps_total_min']} min, {data['denivele_total_m']} m D+"
    ]
    if data.get("vfc"):
        lignes.append(_ligne_vfc(data["vfc"]))
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
        return None, "clÃ© MISTRAL_API_KEY absente (ni variable d'env ni config/mistral.json)"
    prompt = (
        "Tu es un coach sportif. Analyse la semaine d'entraÃ®nement Strava ci-dessous. "
        "Exploite les donnÃ©es de frÃ©quence cardiaque, vitesse, puissance et cadence quand "
        "elles sont prÃ©sentes pour Ã©valuer l'intensitÃ© rÃ©lle des efforts. Exploite aussi "
        "la VFC (variabilitÃ© de frÃ©quence cardiaque, rMSSD nocturne), la FC de repos et le "
        "sommeil d'intervals.icu quand ils sont prÃ©sents pour Ã©valuer la rÃ©cupÃ©ration et "
        "l'Ã©tat de forme : signale les baisses de VFC ou Ã©lÃ©vations de FC de repos qui "
        "suggÃ¨rent un surmenage ou un sommeil insuffisant. "
        "RÃ©ponds en franjÃ§ais, en 150 mots maximum : 2-3 points forts, 1-2 points de "
        "vigilance, 1 conseil concret pour la semaine suivante. Ton direct et encourageant, "
        "pas de listes Ã  puces, du texte fluide en 2 paragraphes courts.\n\n"
        "SEMAINE Ã ANALYSER :\n" + _resume_court(data)
    )
    if precedent:
        prompt += "\n\nSEMAINE PRÃCÃDENTE (pour comparaison) :\n" + _resume_court(precedent)

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
            derniere_erreur = f"HTTP {e.code} (modÃ¨le {modele}) : {detail}"
        except (urllib.error.URLError, KeyError, IndexError) as e:
            derniere_erreur = f"{type(e).__name__} (modÃ¨le {modele}) : {e}"
    return None, derniere_erreur or "erreur inconnue"


def main() -> None:
    if not _heure_paris_valide():
        print(f"Pas encore {HEURE_EXECUTION}h Ã  Paris â rien Ã  faire (ajustement heure Ã©tÃ©/hiver).")
        return

    config = json.loads(CONFIG_PATH.read_text())

    if config.get("refresh_token"):
        resp = http_post(TOKEN_URL, {
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],            "grant_type": "refresh_token",
            "refresh_token": config["refresh_token"],
        })
    elif config.get("auth_code"):
        resp = http_post(TOKEN_URL, {
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],            "grant_type": "authorization_code",
            "code": config["auth_code"],
        })
    else:
        raise SystemExit("Ni refresh_token ni auth_code dans config/strava.json")

    access_token = resp["access_token"]
    if resp.get("refresh_token"):
        config["refresh_token"] = resp["refresh_token"]
        config["auth_code"] = None
        CONFIG_PATH.write_text(json.dumps(config, indent=2))

    after = int((tdt.datetime.now((tdt.timezone.utc) - dt.dimedelta(days=DAYS)).timestamp())
    headers = {"Authorization": f"Bearer {access_token}"}"
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

    # VFC/sommeil intervals.icu (best effort : absent si le job quotidien
    # n'a pas encore tournÃ© ou si la clÃ© API n'est pas configurÃ©)
    vfc = _vfc_semaine()
    if vfc:
        result["vfc"] = vfc
    else:
        result["vfc"] = None

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
        duree = f"{h}{m:02d}" if h else f"{m} min"
        lignes = [
            fÂ§ðê\Ý[pêHÝ]H\ÈÑVTßH\Y\ÈÝ\È®(
Û[\ÙY
_HXÝ]]0êJÊKÝÝ[ÚÛ_HÛKÙ\Y_KÝÝ[Ù\ßßHHH
ÈBYÝ[ÚØØ[YÛ\Ë\[
° )âÝÝ[ÚØØ[HØØ[°îÛ0êY\ÈBYÈ[ËÙ]
×Û[ÞY[WÛ\ÈNYÛ\Ë\[
®((8§i;î#ÈÈ[ÞY[H
ÈHÝÖÉÝ×Û[ÞY[WÛ\É×_H\ÈÝËÙ]
	Ý×ÛZ[Û\ÉÊ_x $ÞÝËÙ]
	Ý×ÛX^Û\ÉÊ_JKÈ\ÜÈÝËÙ]
	Ù×Ü\Ü×Û[ÞY[IÊ_HKÛÛ[YZ[[ÞY[ÝËÙ]
	ÜÛÛ[YZ[Û[ÞY[Ú	Ê_H
BÈ°ê\Ý[pêHÛÛZ\[Y[0ê\\°êH
[X[HHÞ\[HÌLÌHÈ\K\Ý[ÙH][\È[\]Y[Y[Ý]\È\ÈÛ°êY\È0ê]Z[0êY\ÂÈ
Ë]\ÜÙ\ËZ\ÜØ[ÙKØY[ÙKØ[ÜY\ËË]ËH\Ý[ÛÛXÝ0êY\ÂÈ]\Ú]°êY\È[È]\ÝÛÛ]Ý[[X\Y\ËÚ\ÝÜKÈÝ\[[\ÙH[0ê\Y]\KÈ]	Ø[[\ÙHPHÛÛ[YHH\È^Ú]\Ü[\ÙYYÛ\Ë\[
HÜÉÝ\I×_HÜÉÚÛI×_HÛKÜÉÛZ[]\É×_HZ[BY[[\ÙNYÛ\È
ÏHÈ¼'éè[[\ÙHPH[[\ÙWB[ÙNYÛ\È
ÏHÈ° )è[[\ÙHPH[\ÜÛXH8 %Z\ÛÛÜZ\ÛÛÙ\]\HB[ÙNYÛ\ÈHÙ]XÝ[HXÝ]]0êHÝ]HÝ\\ÈÑVTßH\Y\ÈÝ\Ë0êK]ÚHHØ[\0êHH<'ãàÈB
ÕUÑTÈÝ[[X\KKÜ]WÝ^
Ú[YÛ\ÊJBÈ[ÚH[XZ[
\ÝYÜÚ[\Û\[XZ\ÈH°ê\Ý[pêJB\]\Ù[XZ[HÙ[ÚWÙ[XZ[
Ú[YÛ\ÊJBY\]\Ù[XZ[[
[XZ[Û[ÞpêH8 %Z\ÛÛÙ\]\Ù[XZ[HB\Ý[È[XZ[Ù\]\HH\]\Ù[XZ[
ÕUÑTÈ]\ÝÛÛKÜ]WÝ^
ÛÛ[\Ê\Ý[[Ý\WØ\ØÚZOQ[ÙK[[LJB[ÙN[
°ê\Ý[pêH[ÞpêH\[XZ[8§"»î#ÈB[
Ú[YÛ\ÊJBY×Û[YW×ÈOH×ÛXZ[×ÈXZ[
B