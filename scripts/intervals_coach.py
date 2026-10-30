#!/usr/bin/env python3
"""Coach quotidien — deux séances (vélo + course à pied) adaptées à la forme.

Exécuté par GitHub Actions (.github/workflows/intervals-daily.yml) juste après
scripts/intervals_hrv.py, chaque matin à 7h30 heure de Paris (créneau
7h00-9h00 — le cron GitHub partant avec ~45 min de retard, l'email part en
pratique vers 8h15).

Pourquoi ce script (27/10/2026) : la tâche planifiée Vibe « Coach sportif
quotidien » ne peut PAS envoyer d'email — les tâches planifiées tournent sans
surveillance et l'envoi Gmail y exige une approbation humaine (onglet
« Functions » du connecteur Gmail non encore déployé sur le compte). L'email
part donc ici via SMTP Gmail, exactement comme le résumé hebdo du dimanche :
fiable, zéro approbation.

Sources (100 % intervals.icu, archives locales écrites par intervals_hrv.py,
aucun appel API direct ; tout en best effort — une donnée manquante n'empêche
jamais l'email) :
- summaries/intervals/week.json         : VFC, FC repos, sommeil, CTL/ATL/TSB (7 j)
- summaries/intervals/activities.json   : activités des 14 derniers jours (brut API)
- summaries/intervals/tendances.json   : tendances 4-8 semaines (si présent)
- summaries/monthly/records.json       : records (si présent)

Fonctionnement :
1. Digère les données : VFC veille vs moyenne 7 j (baisse > 10 % = fatigue),
   FC repos, sommeil, CTL/ATL/TSB, volume et charge vélo/course sur 7 et 14 j,
   tendances, records.
2. Génère DEUX séances complètes (une VÉLO et une COURSE À PIED) via l'API
   Mistral (modèles de secours : mistral-small-latest → open-mistral-nemo →
   ministral-3b-latest), avec la priorité du jour indiquée sans l'imposer.
   Règle demandée par Cyril : TOUJOURS les deux sports chaque matin.
3. Envoie l'email via SMTP (secrets SMTP_USER / SMTP_PASSWORD / MAIL_TO),
   objet « 🏃🚴 Séances du jour — <jour> ».
4. Archive la proposition dans summaries/intervals/coach/<YYYY-MM-DD>.json
   (commitée par l'étape « Enregistrer les résultats » du workflow).

Secours : si l'IA échoue (clé absente, API indisponible), deux séances
endurance génériques sont envoyées avec la raison de l'échec — l'email part
quand même.
"""

import datetime as dt
import json
import os
import pathlib
import smtplib
import ssl
import urllib.error
import urllib.request
import zoneinfo
from email.message import EmailMessage

BASE = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = BASE / "summaries"
INTERVALS_DIR = OUT_DIR / "intervals"
COACH_DIR = INTERVALS_DIR / "coach"
MISTRAL_CONFIG_PATH = BASE / "config" / "mistral.json"
PARIS = zoneinfo.ZoneInfo("Europe/Paris")
# Créneau d'exécution à Paris : planifié 7h30 (le cron GitHub part souvent
# avec ~45 min de retard → l'email part en pratique vers 8h15), élargi à
# 7h00-9h00 le 30/10/2026 pour absorber les retards > 1 h.
CRENEAU_DEBUT = dt.time(7, 0)
CRENEAU_FIN = dt.time(9, 0)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
MODELES_FALLBACK = ["mistral-small-latest", "open-mistral-nemo", "ministral-3b-latest"]
# Jours de repos imposés (demande de Cyril) : aucune séance ces jours-là,
# un email « Jour de repos » est envoyé à la place. Format YYYY-MM-DD.
JOURS_SANS_SPORT = ["2026-10-31", "2026-11-01"]
JOURS_FR_LONG = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"]


def _heure_paris_valide() -> bool:
    """Vrai s'il est entre 7h00 et 9h00 à Paris (le workflow se déclenche aux
    deux créneaux UTC possibles été/hiver, on n'exécute que le bon ; la
    fenêtre de deux heures absorbe le retard du cron GitHub).
    INTERVALS_FORCE_RUN=1 (workflow_dispatch) force l'exécution."""
    if os.environ.get("INTERVALS_FORCE_RUN"):
        return True
    return CRENEAU_DEBUT <= dt.datetime.now(PARIS).time() < CRENEAU_FIN


def _charger(chemin: pathlib.Path):
    """Charge un JSON en best effort : None si absent ou illisible."""
    try:
        return json.loads(chemin.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _premier(a: dict, cles: list):
    """Premier champ non nul parmi des noms candidats (parsing défensif,
    l'API intervals.icu a déjà changé des noms de champs par le passé)."""
    for c in cles:
        v = a.get(c)
        if v is not None:
            return v
    return None


def _est_velo(type_activite: str) -> bool:
    t = (type_activite or "").lower()
    return "ride" in t or "cycl" in t or "velo" in t


def _est_course(type_activite: str) -> bool:
    t = (type_activite or "").lower()
    return "run" in t or "jog" in t or "course" in t


def _parse_legere(a: dict) -> dict | None:
    """Extrait l'essentiel d'une activité brute intervals.icu."""
    metres = _premier(a, ["totalDistance", "distance"])
    secs = _premier(a, ["movingTime", "moving_time", "elapsedTime", "elapsed_time",
                        "totalTime", "duration"])
    date = str(_premier(a, ["start_date_local", "startTime", "start_time"]) or "")[:10]
    try:
        charge = float(_premier(a, ["icu_training_load", "icuTrainingLoad",
                                     "training_load", "trainingLoad"]) or 0)
    except (TypeError, ValueError):
        charge = 0.0
    return {
        "type": a.get("type") or a.get("sport") or "Unknown",
        "date": date,
        "km": round((metres or 0) / 1000, 1),
        "minutes": round((secs or 0) / 60),
        "charge": charge,
        "fc_moyenne": _premier(a, ["average_heartrate", "averageHeartrate"]),
        "puissance_ponderee_w": _premier(a, ["icu_weighted_avg_watts",
                                             "weightedAverageWatts"]),
    }


def _volume(activites: list, jours: int, aujourdhui) -> dict:
    """Volume d'une liste d'activités sur les « jours » derniers jours."""
    limite = (aujourdhui - dt.timedelta(days=jours - 1)).isoformat()
    sel = [a for a in activites if a["date"] and a["date"] >= limite]
    return {
        "seances": len(sel),
        "km": round(sum(a["km"] for a in sel), 1),
        "heures": round(sum(a["minutes"] for a in sel) / 60, 1),
        "charge_entrainement": round(sum(a["charge"] for a in sel), 1),
    }


def _extrait_compact(objet, limite: int = 1200) -> str | None:
    """Dump JSON compact tronqué — utilisé pour tendances.json et records.json
    dont la structure peut évoluer : l'IA s'y retrouve, aucun parsing fragile."""
    if not objet:
        return None
    try:
        s = json.dumps(objet, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    if len(s) > limite:
        return s[:limite] + "…(tronqué)"
    return s


def _digester(aujourdhui) -> dict:
    """Construit le résumé des données du jour, injecté dans le prompt IA."""
    d: dict = {"date": aujourdhui.isoformat()}

    # --- Wellness 7 j (week.json, écrit ce matin par intervals_hrv.py) ---
    week = _charger(INTERVALS_DIR / "week.json") or {}
    jours = [j for j in (week.get("jours") or []) if j.get("date")]
    jours = sorted(jours, key=lambda j: j["date"])
    d["wellness_7j"] = {k: week.get(k) for k in (
        "vfc_moyenne_ms", "vfc_min_ms", "vfc_max_ms", "fc_repos_moyenne",
        "sommeil_moyen_h", "tsb_moyen", "nb_jours_vfc")}
    veille = None
    for j in reversed(jours):
        if j["date"] <= aujourdhui.isoformat():
            veille = j
            break
    d["veille"] = veille  # dernière mesure dispo (VFC/FC repos/sommeil/TSB)
    vfc_veille = (veille or {}).get("vfc_ms")
    vfc_moy = d["wellness_7j"].get("vfc_moyenne_ms")
    if vfc_veille and vfc_moy:
        d["vfc_veille_vs_moy_7j_pct"] = round(
            (vfc_veille - vfc_moy) / vfc_moy * 100, 1)
    # Dernier point de forme connu (CTL = condition, ATL = fatigue, TSB = forme)
    for j in reversed(jours):
        if j.get("ctl") is not None:
            d["ctl_atl_tsb"] = {"ctl": j.get("ctl"), "atl": j.get("atl"),
                                "tsb": j.get("tsb"), "date": j.get("date")}
            break

    # --- Activités 14 j (activities.json, brut API intervals.icu) ---
    acts_data = _charger(INTERVALS_DIR / "activities.json")
    brutes = None
    if isinstance(acts_data, dict):
        brutes = acts_data.get("activites") or acts_data.get("activities")
    elif isinstance(acts_data, list):
        brutes = acts_data
    legeres = [p for p in (_parse_legere(a) for a in (brutes or [])
                           if isinstance(a, dict)) if p]
    legeres = sorted(legeres, key=lambda a: a["date"])
    velo = [a for a in legeres if _est_velo(a["type"])]
    course = [a for a in legeres if _est_course(a["type"])]
    d["volume"] = {
        "7j": {"velo": _volume(velo, 7, aujourdhui),
               "course": _volume(course, 7, aujourdhui)},
        "14j": {"velo": _volume(velo, 14, aujourdhui),
                "course": _volume(course, 14, aujourdhui)},
    }
    # Détail des 7 derniers jours (une ligne courte par activité)
    limite7 = (aujourdhui - dt.timedelta(days=6)).isoformat()
    d["activites_7j"] = [
        f"{a['date']} {a['type']} : {a['km']} km, {a['minutes']} min"
        + (f", charge {a['charge']:.0f}" if a["charge"] else "")
        + (f", FC moy {a['fc_moyenne']:.0f}" if a["fc_moyenne"] else "")
        + (f", puis. pond. {a['puissance_ponderee_w']:.0f} W"
           if a["puissance_ponderee_w"] else "")
        for a in legeres if a["date"] and a["date"] >= limite7
    ]

    # --- Bloc de repos imposé à venir (adapte l'intensité) ---
    # Trêve ≤ 3 jours : juste avant, on peut charger un peu plus ; le repos
    # total qui suit assure la récupération.
    repos_prochains = [j for j in JOURS_SANS_SPORT
                       if aujourdhui < dt.date.fromisoformat(j)
                       <= aujourdhui + dt.timedelta(days=3)]
    if repos_prochains:
        d["repos_imposes_prochains"] = {
            "jours": repos_prochains,
            "note": ("repos complet décidé par Cyril ces jours-là ; juste "
                     "avant la trêve, une charge un peu plus élevée ou une "
                     "séance de qualité est pertinente"),
        }

    # --- Tendances (4-8 semaines) et records : best effort, dump compact ---
    d["tendances_semaines"] = _extrait_compact(
        _charger(INTERVALS_DIR / "tendances.json"), 1200)
    d["records"] = _extrait_compact(
        _charger(OUT_DIR / "monthly" / "records.json"), 1500)
    return d


def _construire_prompt(digest: dict) -> str:
    return (
        "Tu es le coach sportif personnel de Cyril. Il pratique le vélo et la "
        "course à pied (Garmin epix Gen2, Garmin Edge 850 avec capteur de "
        "puissance et de cadence au vélo). Chaque matin, tu lui proposes DEUX "
        "séances complètes — une VÉLO et une COURSE À PIED — adaptées à sa "
        "forme du jour : il choisit ensuite selon la météo et ses horaires.\n"
        "RÈGLES :\n"
        "- TOUJOURS les deux séances, jamais une seule ni un « vélo OU course ».\n"
        "- Indique la PRIORITÉ DU JOUR (la séance à privilégier s'il n'en fait "
        "qu'une) sans imposer le choix. Maximum 2-3 séances de qualité par "
        "sport et par semaine : alterne qualité et endurance fondamentale.\n"
        "- Adapte l'intensité à la forme : VFC de la veille en baisse de plus "
        "de 10 % vs moyenne 7 j, sommeil insuffisant ou TSB très négatif "
        "(< -20) → séances douces/endurance, voire repos ; VFC stable ou en "
        "hausse et TSB positif → une séance de qualité est possible.\n"
        "- Chaque séance : objectif, durée, échauffement, contenu précis "
        "(séries, zones de FC, puissance en W au vélo), retour au calme.\n"
        "- Si les tendances sont fournies : orients-en la priorité du jour et "
        "annonce la tendance en une phrase.\n"
        "- Si « repos_imposes_prochains » figure dans les données : un bloc "
        "de repos complet décidé par Cyril arrive sous peu. Juste avant la "
        "trêve, une séance de qualité ou un peu plus longue est pertinente "
        "(la récupération est assurée juste après) ; mentionne la trêve à "
        "venir en une phrase pour que Cyril organise sa semaine.\n"
        "- Si un record de moins de 2 semaines apparaît dans les données : "
        "mentionne-le brièvement, de façon motivante.\n"
        "- Réponds en français, format EMAIL en texte brut (pas de markdown, "
        "pas d'astérisques), direct et court :\n"
        "1. une ligne d'état de forme (VFC veille, sommeil, TSB) ;\n"
        "2. « 🚴 SÉANCE VÉLO — <nom> » + « (priorité du jour) » si applicable, "
        "contenu en 4-6 lignes ;\n"
        "3. « 🏃 SÉANCE COURSE À PIED — <nom> » + priorité si applicable, "
        "contenu en 4-6 lignes ;\n"
        "4. 1-2 lignes de justification basées sur les données (charge 7 j, "
        "tendances).\n\n"
        "DONNÉES DU JOUR (intervals.icu, JSON) :\n"
        + json.dumps(digest, ensure_ascii=False)
    )


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


def _generer(prompt: str) -> tuple[str | None, str | None, str | None]:
    """Appelle l'API Mistral avec modèles de secours.
    Retourne (texte, modele, erreur) — texte None si tout a échoué."""
    key = _cle_mistral()
    if not key:
        return None, None, "clé MISTRAL_API_KEY absente (ni env ni config/mistral.json)"
    derniere_erreur = None
    for modele in MODELES_FALLBACK:
        payload = json.dumps({
            "model": modele,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 900,
            "temperature": 0.6,
        }).encode()
        req = urllib.request.Request(
            MISTRAL_CHAT_URL, data=payload, method="POST", headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            })
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = json.loads(resp.read().decode())
            return body["choices"][0]["message"]["content"].strip(), modele, None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            derniere_erreur = f"HTTP {e.code} (modèle {modele}) : {detail}"
        except (urllib.error.URLError, KeyError, IndexError) as e:
            derniere_erreur = f"{type(e).__name__} (modèle {modele}) : {e}"
    return None, None, derniere_erreur or "erreur inconnue"


def _seances_secours(digest: dict, raison: str) -> str:
    """Séances endurance génériques si l'IA est indisponible — l'email part
    quand même, avec la raison de l'échec."""
    vfc = (digest.get("veille") or {}).get("vfc_ms")
    sommeil = (digest.get("veille") or {}).get("sommeil_h")
    return (
        "⚠️ Analyse IA indisponible aujourd'hui — séances endurance "
        "génériques ci-dessous.\n"
        f"(Raison : {raison})\n"
        "\n"
        "🚴 SÉANCE VÉLO — Endurance fondamentale : 60 à 90 min en zone 2 "
        "(allure conversationnelle), cadence 85-95 tr/min, avec 3 blocs de "
        "5 min à 95-100 tr/min (5 min faciles entre chaque). Retour au calme "
        "10 min en moulinant souple.\n"
        "\n"
        "🏃 SÉANCE COURSE À PIED — Endurance douce : 40 à 50 min allure "
        "facile (zone 2, respiration calme), 3 accélérations progressives de "
        "20 s dans les 10 dernières minutes. Retour au calme 5-10 min.\n"
        "\n"
        "Écoute ton corps : VFC de la veille en baisse ou sommeil insuffisant "
        "→ raccourcis les séances ou prends du repos.\n"
        + (f"\n(VFC veille : {vfc} ms, sommeil : {sommeil} h — données partielles.)\n"
           if vfc or sommeil else "")
    )


def _envoi_email(contenu: str, sujet: str) -> str | None:
    """Envoie l'email via SMTP (Gmail par défaut). Retourne None si succès,
    ou la raison de l'échec. Best effort."""
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    to = os.environ.get("MAIL_TO") or user
    if not user or not password:
        return "secrets SMTP_USER / SMTP_PASSWORD absents — email non envoyé"
    msg = EmailMessage()
    msg["Subject"] = sujet
    msg["From"] = f"Coach Bot <{user}>"
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


def main() -> None:
    if not _heure_paris_valide():
        print("Hors créneau 7h00-9h00 à Paris — pas de séances aujourd'hui "
              "(ajustement heure été/hiver).")
        return

    maintenant = dt.datetime.now(PARIS)
    aujourdhui = maintenant.date()

    # --- Jour sans sport (repos imposé, décidé par Cyril) ---
    if aujourdhui.isoformat() in JOURS_SANS_SPORT:
        sujet = (f"🧘 Jour de repos — {JOURS_FR_LONG[aujourdhui.weekday()]} "
                 f"{aujourdhui.day} {MOIS_FR[aujourdhui.month - 1]}")
        texte = (
            "Repos complet aujourd'hui : journée sans sport décidée par "
            "Cyril — ni vélo, ni course à pied.\n"
            "Profites-en pour bien récupérer : hydratation, étirements "
            "doux, marche légère si besoin, et un bon sommeil.\n"
            "Les séances reprennent le lendemain matin comme d'habitude."
        ) + "\n\n— Ton coach (GitHub Actions + intervals.icu)"
        erreur_email = _envoi_email(texte, sujet)
        COACH_DIR.mkdir(parents=True, exist_ok=True)
        enregistrement = {
            "date": aujourdhui.isoformat(),
            "genere_le": dt.datetime.now(dt.timezone.utc).isoformat(),
            "sujet": sujet,
            "repos_impose": True,
            "seances": texte,
            "email_erreur": erreur_email,
        }
        (COACH_DIR / f"{aujourdhui.isoformat()}.json").write_text(
            json.dumps(enregistrement, ensure_ascii=False, indent=2))
        if erreur_email:
            print(f"⚠️ Email non envoyé : {erreur_email}")
        else:
            print("Jour de repos imposé — email repos envoyé ✉️")
        return

    sujet = (f"🏃🚴 Séances du jour — {JOURS_FR_LONG[aujourdhui.weekday()]} "
             f"{aujourdhui.day} {MOIS_FR[aujourdhui.month - 1]}")

    digest = _digester(aujourdhui)
    print(f"Données du jour : veille={digest.get('veille') and 'oui' or 'inconnue'}, "
          f"volume 7j vélo={digest['volume']['7j']['velo']['km']} km, "
          f"course={digest['volume']['7j']['course']['km']} km")

    texte, modele, erreur_ia = _generer(_construire_prompt(digest))
    if texte is None:
        print(f"⚠️ IA indisponible : {erreur_ia}")
        texte = _seances_secours(digest, erreur_ia or "erreur inconnue")
        pied = "\n\n— Ton coach (GitHub Actions + intervals.icu)"
    else:
        pied = f"\n\n— Ton coach (GitHub Actions + intervals.icu, modèle {modele})"
    texte += pied

    erreur_email = _envoi_email(texte, sujet)

    # Archive locale : summaries/intervals/coach/<date>.json — commitée par
    # l'étape « Enregistrer les résultats » du workflow (git add summaries/intervals/).
    COACH_DIR.mkdir(parents=True, exist_ok=True)
    enregistrement = {
        "date": aujourdhui.isoformat(),
        "genere_le": dt.datetime.now(dt.timezone.utc).isoformat(),
        "sujet": sujet,
        "donnees": digest,
        "seances": texte,
        "modele_ia": modele,
        "ia_erreur": erreur_ia,
        "email_erreur": erreur_email,
    }
    (COACH_DIR / f"{aujourdhui.isoformat()}.json").write_text(
        json.dumps(enregistrement, ensure_ascii=False, indent=2))

    if erreur_email:
        print(f"⚠️ Email non envoyé : {erreur_email}")
    else:
        print("Email envoyé ✉️")
    print(f"Modèle IA : {modele or 'secours (séances génériques)'}")
    print("--- Séances du jour ---")
    print(texte)


if __name__ == "__main__":
    main()

