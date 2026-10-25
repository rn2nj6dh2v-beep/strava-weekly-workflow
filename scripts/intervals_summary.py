#!/usr/bin/env python3
"""Résumé hebdomadaire intervals.icu — exécuté par GitHub Actions le dimanche 23h (heure de Paris).

Remplace scripts/strava_summary.py (bascule 100 % intervals.icu demandée par
Cyril le 25/10/2026) : plus AUCUNE donnée Strava.

- Activités des 7 derniers jours via GET /api/v1/athlete/{id}/activities
- Wellness des 7 derniers jours via GET /api/v1/athlete/{id}/wellness
  (VFC rMSSD, FC repos, sommeil, CTL/ATL/TSB)
- Auth/User-Agent réutilisés depuis scripts/intervals_hrv.py (auth Basic
  "API_KEY":clé + User-Agent navigateur, sinon Cloudflare bloque)
- Archivage hebdomadaire dans summaries/history/<année>-S<semaine ISO>.json
- Analyse IA via l'API Mistral (comparaison avec la semaine précédente archivée)
- Résumé épuré (type/distance/temps) : les données détaillées restent
  archivées dans latest.json (champ "brut" = réponse exacte de l'API par
  activité, rien n'est perdu) et exploitées par l'analyse IA
- Envoi du résumé par email (SMTP) si SMTP_USER/SMTP_PASSWORD/MAIL_TO présents
"""
import datetime as dt
import json
import os
import pathlib
import smtplib
import ssl
import statistics
import urllib.error
import urllib.request
from email.message import EmailMessage
import zoneinfo

from intervals_hrv import _get, _athlete_id, _parse_jour  # même dossier

BASE = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = BASE / "summaries"
HISTORY_DIR = OUT_DIR / "history"
MISTRAL_CONFIG_PATH = BASE / "config" / "mistral.json"
DAYS = 7
PARIS = zoneinfo.ZoneInfo("Europe/Paris")
HEURE_EXECUTION = 23  # heure de Paris
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
MODELES_FALLBACK = ["mistral-small-latest", "open-mistral-nemo", "ministral-3b-latest"]


def _heure_paris_valide() -> bool:
    """Vrai s'il est HEURE_EXECUTION à Paris (ajustement été/hiver : GitHub
    déclenche aux deux créneaux UTC possibles, on n'exécute que le bon)."""
    if os.environ.get("INTERVALS_FORCE_RUN"):
        return True  # déclenchement manuel : toujours exécuter
    return dt.datetime.now(PARIS).hour == HEURE_EXECUTION


def _premier(a: dict, cles: list):
    """Premier champ non nul parmi des noms candidats. L'API intervals.icu a
    déjà changé des noms de champs par le passé : parsing défensif."""
    for c in cles:
        v = a.get(c)
        if v is not None:
            return v
    return None


def _peut_etre(v, arrondi=None):
    if v is None or v == 0:
        return None
    return round(v, arrondi) if arrondi is not None else v


def _parse_activite(a: dict) -> dict:
    metres = _premier(a, ["totalDistance", "distance", "total_distance"])
    secs = _premier(a, ["totalTime", "duration", "movingTime", "elapsed_time"])
    vitesse_moy = _premier(a, ["averageSpeed", "average_speed"])
    vitesse_max = _premier(a, ["maxSpeed", "max_speed"])
    return {
        "nom": a.get("name") or "Sans nom",
        "type": a.get("type") or a.get("sport") or "Unknown",
        "date": str(_premier(a, ["startTime", "start_time", "start_date_local"]) or "")[:10],
        "km": round((metres or 0) / 1000, 2),
        "minutes": round((secs or 0) / 60),
        "denivele_m": _peut_etre(_premier(a, [
            "elevationGain", "elevation_gain", "totalElevationGain", "total_elevation_gain",
        ]), 1),
        "fc_moyenne": _peut_etre(_premier(a, ["averageHeartrate", "average_heartrate"])),
        "fc_max": _peut_etre(_premier(a, ["maxHeartrate", "max_heartrate"])),
        "vitesse_moyenne_kmh": _peut_etre((vitesse_moy or 0) * 3.6, 1) if vitesse_moy else None,
        "vitesse_max_kmh": _peut_etre((vitesse_max or 0) * 3.6, 1) if vitesse_max else None,
        "puissance_moyenne_w": _peut_etre(_premier(a, ["averageWatts", "average_watts"])),
        "puissance_max_w": _peut_etre(_premier(a, ["maxWatts", "max_watts"])),
        "puissance_ponderee_w": _peut_etre(_premier(a, [
            "weightedAverageWatts", "weighted_average_watts", "icu_weighted_avg_watts",
        ])),
        "cadence_moyenne": _peut_etre(_premier(a, ["averageCadence", "average_cadence"]), 1),
        "cadence_max": _peut_etre(_premier(a, ["maxCadence", "max_cadence"]), 1),
        "charge_entrainement": _peut_etre(_premier(a, [
            "icuTrainingLoad", "icu_training_load", "trainingLoad", "training_load",
        ])),
        "appareil": _premier(a, ["deviceName", "device_name"]) or None,
        "brut": a,  # réponse exacte de l'API : toutes les données conservées
    }


def _moy(vals: list, arrondi=1):
    vals = [v for v in vals if v is not None]
    return round(statistics.mean(vals), arrondi) if vals else None


def _stats_wellness(jours: list[dict]) -> dict | None:
    """Stats VFC/sommeil/CTL-ATL des jours de la fenêtre (données intervals.icu)."""
    if not jours:
        return None
    vfcs = [j["vfc_ms"] for j in jours if j.get("vfc_ms") is not None]
    return {
        "nb_jours_vfc": len(vfcs),
        "vfc_moyenne_ms": _moy(vfcs),
        "vfc_min_ms": min(vfcs) if vfcs else None,
        "vfc_max_ms": max(vfcs) if vfcs else None,
        "fc_repos_moyenne": _moy([j.get("fc_repos") for j in jours]),
        "sommeil_moyen_h": _moy([j.get("sommeil_h") for j in jours]),
        "ctl_dernier": jours[-1].get("ctl"),
        "atl_dernier": jours[-1].get("atl"),
        "tsb_dernier": jours[-1].get("tsb"),
        "jours": jours,
    }


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
    sujet = f"📊 Ton résumé sportif de la semaine — {dt.datetime.now(PARIS).strftime('%d/%m/%Y')}"
    msg = EmailMessage()
    msg["Subject"] = sujet
    msg["From"] = f"Intervals Bot <{user}>"
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


def _resume_court(data: dict) -> str:
    """Version détaillée pour l'analyse IA uniquement (jamais affichée)."""
    lignes = [
        f"{data['nb_activites']} activités, {data['distance_totale_km']} km, "
        f"{data['temps_total_min']} min, {data['denivele_total_m']} m D+"
    ]
    for t, v in data.get("par_type", {}).items():
        lignes.append(f"  {t}: {v['nombre']} sorties, {v['km']} km")
    for a in data.get("activites", []):
        extras = []
        if a.get("fc_moyenne"):
            extras.append(f"FC {a['fc_moyenne']}/{a.get('fc_max')} bpm")
        if a.get("vitesse_moyenne_kmh"):
            extras.append(f"{a['vitesse_moyenne_kmh']} km/h moy")
        if a.get("puissance_moyenne_w"):
            extras.append(f"{a['puissance_moyenne_w']} W moy")
        if a.get("puissance_ponderee_w"):
            extras.append(f"{a['puissance_ponderee_w']} W pondéré")
        if a.get("cadence_moyenne"):
            extras.append(f"cadence {a['cadence_moyenne']}")
        if a.get("charge_entrainement"):
            extras.append(f"charge {a['charge_entrainement']}")
        if a.get("denivele_m"):
            extras.append(f"{a['denivele_m']} m D+")
        detail = f"  {a['date']} {a['type']} : {a['km']} km, {a['minutes']} min"
        if extras:
            detail += " [" + ", ".join(str(e) for e in extras) + "]"
        lignes.append(detail)
    v = data.get("vfc")
    if v and v.get("vfc_moyenne_ms"):
        lignes.append(
            f"  VFC (intervals.icu) : moyenne {v['vfc_moyenne_ms']} ms sur "
            f"{v['nb_jours_vfc']} jour(s) (min {v['vfc_min_ms']}, max {v['vfc_max_ms']}), "
            f"FC repos {v['fc_repos_moyenne']} bpm, sommeil moyen {v['sommeil_moyen_h']} h"
        )
        for j in v.get("jours", []):
            if j.get("vfc_ms") is not None:
                lignes.append(
                    f"    {j['date']} : VFC {j.get('vfc_ms')} ms, FC repos "
                    f"{j.get('fc_repos')}, sommeil {j.get('sommeil_h')} h"
                )
    return "\n".join(lignes)


def _analyse_ia(data: dict, precedent: dict | None) -> tuple[str | None, str | None]:
    key = _cle_mistral()
    if not key:
        return None, "clé MISTRAL_API_KEY absente (ni variable d'env ni config/mistral.json)"
    prompt = (
        "Tu es un coach sportif. Analyse la semaine d'entraînement ci-dessous (données "
        "intervals.icu). Exploite les données de fréquence cardiaque, vitesse, puissance "
        "et cadence quand elles sont présentes pour évaluer l'intensité réelle des efforts. "
        "Exploite aussi la VFC (variabilité de fréquence cardiaque, rMSSD nocturne), la FC "
        "de repos et le sommeil pour évaluer la récupération et l'état de forme : signale "
        "les baisses de VFC ou élévations de FC de repos qui suggèrent un surmenage ou un "
        "sommeil insuffisant. "
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

    athlete_id = _athlete_id()
    print(f"Athlete intervals.icu : {athlete_id}")

    aujourd_hui = dt.datetime.now(PARIS).date()
    plus_ancien = aujourd_hui - dt.timedelta(days=DAYS - 1)
    fenetre = {"oldest": plus_ancien.isoformat(), "newest": aujourd_hui.isoformat()}

    # Activités : source requise — un échec interrompt (comme l'ancien flux Strava)
    activites_brutes = _get(f"/athlete/{athlete_id}/activities", fenetre)
    if not isinstance(activites_brutes, list):
        print("RÉPONSE INATTENDUE de l'API activités — débogage :")
        print(json.dumps(activites_brutes, ensure_ascii=False)[:1500])
        raise SystemExit("ERREUR INTERVALS : réponse activités d'un type inattendu")

    # Wellness : best effort (le résumé continue sans VFC si indisponible)
    jours = []
    erreur_vfc = None
    try:
        wellness = _get(f"/athlete/{athlete_id}/wellness", fenetre)
        if isinstance(wellness, list):
            jours = [_parse_jour(w) for w in wellness if isinstance(w, dict)]
            jours = [j for j in jours if j.get("date")]
            jours.sort(key=lambda j: j["date"])
    except SystemExit as e:
        erreur_vfc = str(e)
        print(f"⚠️ Wellness non récupéré (résumé sans VFC) : {erreur_vfc}")

    parsed = [_parse_activite(a) for a in activites_brutes if isinstance(a, dict)]
    parsed.sort(key=lambda p: (p["date"], p["nom"]))

    total_km = round(sum(p["km"] for p in parsed), 2)
    total_min = sum(p["minutes"] for p in parsed)
    total_dplus = round(sum(p["denivele_m"] or 0 for p in parsed), 1)
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
        "source": "intervals.icu",
        "fenetre_jours": DAYS,
        "periode": {"debut": plus_ancien.isoformat(), "fin": aujourd_hui.isoformat()},
        "nb_activites": len(parsed),
        "distance_totale_km": total_km,
        "temps_total_min": total_min,
        "denivele_total_m": total_dplus,
        "par_type": par_type,
        "activites": parsed,
        "vfc": _stats_wellness(jours),
        "vfc_erreur": erreur_vfc,
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

    # Résumé volontairement épuré (demande de Cyril du 24/10/2026) :
    # type, distance et temps uniquement. Toutes les données détaillées restent
    # archivées dans latest.json et summaries/history/ et exploitées par l'IA.
    if parsed:
        h, m = divmod(total_min, 60)
        duree = f"{h}h{m:02d}" if h else f"{m} min"
        lignes = [
            f"📊 Résumé sportif des {DAYS} derniers jours (intervals.icu)",
            f"• {len(parsed)} activité(s), {total_km} km, {duree}, {total_dplus} m de D+",
        ]
        vfc = result.get("vfc")
        if vfc and vfc.get("vfc_moyenne_ms"):
            lignes.append(
                f"• ❤️ VFC moyenne (7 j) : {vfc['vfc_moyenne_ms']} ms "
                f"({vfc.get('vfc_min_ms')}–{vfc.get('vfc_max_ms')}), "
                f"FC repos {vfc.get('fc_repos_moyenne')} bpm, "
                f"sommeil moyen {vfc.get('sommeil_moyen_h')} h"
            )
        for p in parsed:
            lignes.append(f"  - {p['type']} : {p['km']} km, {p['minutes']} min")
        if analyse:
            lignes += ["", "🧠 Analyse IA :", analyse]
        else:
            lignes += ["", f"🧠 Analyse IA : indisponible — raison : {raison_erreur}"]
    else:
        lignes = [f"Aucune activité sur les {DAYS} derniers jours. Lève-toi du canapé ! 🏃"]
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
