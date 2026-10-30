#!/usr/bin/env python3
"""Récupération quotidienne des données wellness intervals.icu (VFC/HRV, sommeil, etc.)

- Authentification Basic : nom d'utilisateur littéral "API_KEY",
  mot de passe = clé API (secret INTERVALS_API_KEY)
- Identifiant athlete : config/intervals.json (ou "0" = soi-même)
- Archivage mensuel cumulé : summaries/intervals/<YYYY>-MM.json (fusion par date)
- summaries/intervals/week.json : statistiques des 7 derniers jours, lues par
  scripts/strava_summary.py pour enrichir le résumé hebdomadaire du dimanche.
- summaries/intervals/activities.json : activités d'entraînement des 14 derniers
  jours (endpoint /athlete/{id}/activities), données brutes intervals.icu —
  UNIQUE source d'activités du coach quotidien Vibe (Strava abandonné pour le
  coach le 25/10/2026, demande de Cyril). Best effort : un échec de cet appel
  n'interrompt jamais l'archivage wellness.
- summaries/intervals/activities_archive/<YYYY>-MM.json : ARCHIVE CUMULÉE
  ILLIMITÉE des activités (demande de Cyril du 26/10/2026). Chaque activité est
  fusionnée par identifiant dans le fichier du mois de sa date : rien n'est
  jamais supprimé, et une activité modifiée écrase son ancienne version. Le
  cadre de rattrapage INTERVALS_ACTIVITES_BACKFILL_JOURS (défaut 365, 0 =
  désactivé, ex. 730 via workflow_dispatch pour aller plus loin) récupère
  automatiquement les mois manquants de l'historique intervals.icu. Le coach
  du matin continue de lire activities.json (fenêtre glissante 14 jours) ;
  l'archive sert aux analyses long terme.
- Exécuté par GitHub Actions à 7h30 heure de Paris (double cron été/hiver,
  le script vérifie l'heure réelle à Paris — créneau 7h00-9h00 toléré pour
  absorber le retard systématique du cron GitHub, ~45 min).
- INTERVALS_BACKFILL_DAYS (défaut 3) : fenêtre de récupération wellness — permet le
  rattrapage historique (ex. 90 jours) via workflow_dispatch.
"""
import base64
import datetime as dt
import json
import os
import pathlib
import statistics
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo

BASE = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = BASE / "summaries" / "intervals"
ARCHIVE_DIR = OUT_DIR / "activities_archive"
CONFIG_PATH = BASE / "config" / "intervals.json"
API = "https://intervals.icu/api/v1"
JOURS_FENETRE = int(os.environ.get("INTERVALS_BACKFILL_DAYS", "3"))  # jours récupérés (rattrapage possible)
JOURS_SEMAINE = 7  # fenêtre des stats hebdo écrites dans week.json
JOURS_ACTIVITES = 14  # fenêtre des activités archivées dans activities.json
JOURS_ARCHIVE_BACKFILL = int(os.environ.get("INTERVALS_ACTIVITES_BACKFILL_JOURS", "365"))  # horizon de l'archive cumulée
PARIS = zoneinfo.ZoneInfo("Europe/Paris")
# Créneau d'exécution à Paris : planifié 7h30 (le cron GitHub part souvent
# avec ~45 min de retard et parfois plus), élargi à 7h00-9h00 le 30/10/2026
CRENEAU_DEBUT = dt.time(7, 0)
CRENEAU_FIN = dt.time(9, 0)


def _heure_paris_valide() -> bool:
    """Vrai s'il est entre 7h00 et 9h00 à Paris (ajustement été/hiver :
    GitHub déclenche aux deux créneaux UTC possibles, on n'exécute que le
    bon ; la fenêtre de deux heures absorbe le retard du cron GitHub)."""
    if os.environ.get("INTERVALS_FORCE_RUN"):
        return True  # déclenchement manuel : toujours exécuter
    return CRENEAU_DEBUT <= dt.datetime.now(PARIS).time() < CRENEAU_FIN


def _headers() -> dict:
    key = os.environ.get("INTERVALS_API_KEY")
    if not key:
        raise SystemExit("ERREUR INTERVALS : secret INTERVALS_API_KEY absent")
    # Authentification Basic spécifique intervals.icu : le nom d'utilisateur
    # est littéralement "API_KEY", et le mot de passe est la clé API.
    # (cf. forum intervals.icu — "API access to Intervals.icu")
    token = base64.b64encode(f"API_KEY:{key}".encode()).decode()
    # intervals.icu est derrière Cloudflare : sans User-Agent "navigateur",
    # la requête est bloquée (erreur 1010 Access denied).
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    }


def _get(path: str, params: dict | None = None):
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise SystemExit(f"ERREUR INTERVALS {e.code} : {body}")


def _athlete_id() -> str:
    """Identifiant athlete : config/intervals.json si présent, sinon "0"
    (= soi-même pour l'API intervals.icu, cf. forum officiel)."""
    if CONFIG_PATH.exists():
        try:
            aid = json.loads(CONFIG_PATH.read_text()).get("athlete_id")
            if aid:
                return str(aid)
        except (json.JSONDecodeError, OSError):
            pass
    return "0"


def _date_wellness(w: dict) -> str | None:
    """Extrait la date d'une entrée wellness : la date est le champ 'id'
    (ex. "2026-10-24"), parfois aussi 'localDate' ou 'date'."""
    for cle in ("id", "localDate", "date"):
        v = w.get(cle)
        if isinstance(v, str) and len(v) >= 10 and v[:4].isdigit():
            return v[:10]
    return None


def _parse_jour(w: dict) -> dict:
    sommeil_h = None
    if w.get("sleepSecs"):
        sommeil_h = round(w["sleepSecs"] / 3600, 1)
    elif w.get("sleepMsecs"):
        sommeil_h = round(w["sleepMsecs"] / 3600000, 1)
    # FC de repos : le champ s'appelle restingHR (ou restingHeartRate selon les versions)
    fc_repos = w.get("restingHR")
    if fc_repos is None:
        fc_repos = w.get("restingHeartRate")
    return {
        "date": _date_wellness(w),
        "vfc_ms": w.get("hrv") or None,  # rMSSD en ms, mesuré la nuit par la Garmin
        "fc_repos": fc_repos or None,
        "sommeil_h": sommeil_h,
        "qualite_sommeil": w.get("sleepQuality") or None,
        "fatigue": w.get("fatigue") or None,
        "humeur": w.get("mood") or None,
        "readiness": w.get("readiness") or None,
        "stress": w.get("stress") or None,
        "douleurs": w.get("soreness") or None,
        "poids_kg": w.get("weightKg") or None,
        "ctl": w.get("ctl") or None,      # condition (fitness)
        "atl": w.get("atl") or None,      # fatigue
        "tsb": w.get("tsb") or None,      # forme
    }


def _moy(vals: list, arrondi=1):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return round(statistics.mean(vals), arrondi)


def _archive_mensuelle(jours: list[dict]) -> None:
    """Fusionne les jours dans summaries/intervals/<YYYY>-MM.json, sans écraser
    les mois précédents. Re-exécutable : chaque jour écrase sa propre entrée."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    par_mois: dict[str, dict[str, dict]] = {}
    for j in jours:
        date = j.get("date") or ""
        if not date:
            continue
        par_mois.setdefault(date[:7], {})[date] = j
    for mois, par_date in par_mois.items():
        chemin = OUT_DIR / f"{mois}.json"
        existant: dict[str, dict] = {}
        if chemin.exists():
            try:
                existant = json.loads(chemin.read_text())
            except (json.JSONDecodeError, OSError):
                existant = {}
        existant.update(par_date)
        chemin.write_text(json.dumps(existant, ensure_ascii=False, indent=2, sort_keys=True))


def _date_activite(a: dict) -> str | None:
    """Date locale d'une activité : start_date_local (champ confirmé par l'API
    intervals.icu), sinon startTime/start_time. Retourne YYYY-MM-DD ou None."""
    for cle in ("start_date_local", "startTime", "start_time", "start_date"):
        v = a.get(cle)
        if isinstance(v, str) and len(v) >= 10 and v[:4].isdigit():
            return v[:10]
    return None


def _cle_activite(a: dict) -> str:
    """Clé de fusion d'une activité dans l'archive : son id intervals.icu,
    sinon un composite date|type|distance stable pour un même enregistrement."""
    identifiant = a.get("id")
    if identifiant not in (None, ""):
        return str(identifiant)
    return "|".join(
        str(x) for x in (
            _date_activite(a),
            a.get("type"),
            a.get("distance"),
        )
    )


def _fusionner_archive(activites: list[dict]) -> int:
    """Fusionne des activités dans l'archive cumulée
    summaries/intervals/activities_archive/<YYYY>-MM.json, regroupées par mois
    de leur date locale. Fusion par clé (id) : une activité déjà archivée est
    mise à jour, jamais dupliquée ; les autres mois ne sont jamais perdus."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    par_mois: dict[str, dict[str, dict]] = {}
    sans_date = 0
    for a in activites:
        date = _date_activite(a)
        if not date:
            sans_date += 1
            continue
        par_mois.setdefault(date[:7], {})[_cle_activite(a)] = a
    for mois, par_id in par_mois.items():
        chemin = ARCHIVE_DIR / f"{mois}.json"
        existant: dict[str, dict] = {}
        if chemin.exists():
            try:
                existant = json.loads(chemin.read_text())
            except (json.JSONDecodeError, OSError):
                existant = {}
        existant.update(par_id)
        chemin.write_text(json.dumps(existant, ensure_ascii=False, indent=2, sort_keys=True))
    if sans_date:
        print(f"⚠️ {sans_date} activité(s) sans date lisible — non fusionnée(s) dans l'archive")
    return len(activites) - sans_date


def _mois_manquants(aujourd_hui) -> list[tuple[str, dt.date, dt.date]]:
    """Mois du cadre de rattrapage (JOURS_ARCHIVE_BACKFILL jours) sans fichier
    d'archive, avec la plage de dates à récupérer pour chacun. Le mois courant
    est exclu : il est couvert par les fusions quotidiennes de la fenêtre
    glissante."""
    debut = aujourd_hui - dt.timedelta(days=JOURS_ARCHIVE_BACKFILL - 1)
    mois_courant = aujourd_hui.strftime("%Y-%m")
    manquants: list[tuple[str, dt.date, dt.date]] = []
    curseur = dt.date(debut.year, debut.month, 1)
    while curseur.strftime("%Y-%m") < mois_courant:
        libelle = curseur.strftime("%Y-%m")
        if curseur.month == 12:
            fin_mois = dt.date(curseur.year, 12, 31)
            suivant = dt.date(curseur.year + 1, 1, 1)
        else:
            fin_mois = dt.date(curseur.year, curseur.month + 1, 1) - dt.timedelta(days=1)
            suivant = dt.date(curseur.year, curseur.month + 1, 1)
        if not (ARCHIVE_DIR / f"{libelle}.json").exists():
            manquants.append((libelle, max(curseur, debut), fin_mois))
        curseur = suivant
    return manquants


def _completer_archive(athlete_id: str, aujourd_hui) -> None:
    """Récupère les mois d'activités manquants de l'historique intervals.icu
    dans le cadre du rattrapage (initialisation de l'archive, ou élargissement
    via workflow_dispatch). Best effort : un mois en échec est réessayé au run
    suivant ; un mois sans activité reçoit un fichier vide pour ne pas être
    redemandé chaque jour."""
    manquants = _mois_manquants(aujourd_hui)
    if not manquants:
        return
    print(
        f"Archive cumulée : {len(manquants)} mois à récupérer "
        f"(rattrapage {JOURS_ARCHIVE_BACKFILL} jours)"
    )
    for libelle, plus_ancien, plus_recent in manquants:
        try:
            acts = _get(f"/athlete/{athlete_id}/activities", {
                "oldest": plus_ancien.isoformat(),
                "newest": plus_recent.isoformat(),
            })
        except SystemExit as e:
            print(f"⚠️ Mois {libelle} non récupéré (réessayé au prochain run) : {e}")
            continue
        if not isinstance(acts, list):
            print(f"⚠️ Mois {libelle} : réponse inattendue — ignoré ce run")
            continue
        activites = [a for a in acts if isinstance(a, dict)]
        _fusionner_archive(activites)
        if not activites:
            (ARCHIVE_DIR / f"{libelle}.json").write_text("{}")
        print(f"  {libelle} : {len(activites)} activité(s) archivée(s)")


def _fusionner_mois_courant(athlete_id: str, aujourd_hui) -> int:
    """Récupère TOUT le mois courant en cours (du 1er à aujourd'hui) et le
    fusionne dans l'archive. Garantit qu'aucune activité du mois n'échappe à
    l'archive au-delà de la fenêtre glissante de 14 jours (ex. activités du
    début du mois), et rafraîchit les éditions rétroactives du mois.
    Best effort : un échec est réessayé au run suivant."""
    debut_mois = dt.date(aujourd_hui.year, aujourd_hui.month, 1)
    try:
        acts = _get(f"/athlete/{athlete_id}/activities", {
            "oldest": debut_mois.isoformat(),
            "newest": aujourd_hui.isoformat(),
        })
    except SystemExit as e:
        print(f"⚠️ Mois courant non complété (réessayé au prochain run) : {e}")
        return 0
    if not isinstance(acts, list):
        print("⚠️ Mois courant : réponse inattendue — ignoré ce run")
        return 0
    activites = [a for a in acts if isinstance(a, dict)]
    nb = _fusionner_archive(activites)
    if nb:
        print(f"Mois courant complet : {nb} activité(s) fusionnée(s)")
    return nb


def _archive_activites(athlete_id: str, aujourd_hui) -> None:
    """Récupère les activités des JOURS_ACTIVITES derniers jours (endpoint
    /athlete/{id}/activities) et les archive de deux façons :
    - summaries/intervals/activities.json : fenêtre glissante remplacée à
      chaque exécution, lue par le coach quotidien Vibe ;
    - summaries/intervals/activities_archive/<YYYY>-MM.json : archive cumulée
      illimitée, fusionnée par identifiant (les mois manquants du cadre de
      rattrapage sont récupérés automatiquement).
    Re-exécutable. Best effort : une erreur n'interrompt pas l'archivage
    wellness."""
    plus_ancien = aujourd_hui - dt.timedelta(days=JOURS_ACTIVITES - 1)
    try:
        acts = _get(f"/athlete/{athlete_id}/activities", {
            "oldest": plus_ancien.isoformat(),
            "newest": aujourd_hui.isoformat(),
        })
    except SystemExit as e:
        print(f"⚠️ Activités non récupérées (wellness archivé quand même) : {e}")
        return
    if not isinstance(acts, list):
        print("⚠️ Réponse activités inattendue — débogage :")
        print(json.dumps(acts, ensure_ascii=False)[:800])
        return
    activites = [a for a in acts if isinstance(a, dict)]
    activites.sort(
        key=lambda a: str(a.get("startTime") or a.get("start_time") or a.get("id") or "")
    )
    contenu = {
        "genere_le": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "intervals.icu /athlete/{id}/activities",
        "fenetre_jours": JOURS_ACTIVITES,
        "nb_activites": len(activites),
        "activites": activites,
    }
    (OUT_DIR / "activities.json").write_text(
        json.dumps(contenu, ensure_ascii=False, indent=2)
    )
    print(f"Activités archivées : {len(activites)} sur {JOURS_ACTIVITES} jours")
    for a in activites[-5:]:
        print(f"  {a.get('startTime')} — {a.get('type')} : {a.get('name')}")

    # Archive cumulée illimitée : fusion de la fenêtre du jour + mois courant
    # complet + rattrapage automatique des mois passés manquants (best effort).
    nb_fusionnes = _fusionner_archive(activites)
    nb_courant = _fusionner_mois_courant(athlete_id, aujourd_hui)
    if JOURS_ARCHIVE_BACKFILL > 0:
        _completer_archive(athlete_id, aujourd_hui)
    if nb_fusionnes or nb_courant:
        print(f"Archive cumulée : {nb_fusionnes} activité(s) fenêtre 14 j + {nb_courant} mois courant")


def _stats_semaine() -> dict | None:
    """Lit l'archive mensuelle et calcule les stats des 7 derniers jours."""
    aujourd_hui = dt.datetime.now(PARIS).date()
    debut = aujourd_hui - dt.timedelta(days=JOURS_SEMAINE - 1)
    jours: list[dict] = []
    for offset in range(JOURS_SEMAINE):
        d = debut + dt.timedelta(days=offset)
        chemin = OUT_DIR / f"{d.strftime('%Y-%m')}.json"
        if not chemin.exists():
            continue
        try:
            mois = json.loads(chemin.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        j = mois.get(d.isoformat())
        if j:
            jours.append(j)
    if not jours:
        return None
    vfcs = [j["vfc_ms"] for j in jours if j.get("vfc_ms") is not None]
    return {
        "genere_le": dt.datetime.now(dt.timezone.utc).isoformat(),
        "periode": {"debut": debut.isoformat(), "fin": aujourd_hui.isoformat()},
        "nb_jours": len(jours),
        "nb_jours_vfc": len(vfcs),
        "vfc_moyenne_ms": _moy(vfcs),
        "vfc_min_ms": min(vfcs) if vfcs else None,
        "vfc_max_ms": max(vfcs) if vfcs else None,
        "fc_repos_moyenne": _moy([j.get("fc_repos") for j in jours]),
        "sommeil_moyen_h": _moy([j.get("sommeil_h") for j in jours]),
        "qualite_sommeil_moyenne": _moy([j.get("qualite_sommeil") for j in jours]),
        "readiness_moyen": _moy([j.get("readiness") for j in jours]),
        "tsb_moyen": _moy([j.get("tsb") for j in jours]),
        "jours": sorted(jours, key=lambda j: j.get("date") or ""),
    }


def main() -> None:
    if not _heure_paris_valide():
        print("Hors créneau 7h00-9h00 à Paris — rien à faire (ajustement heure été/hiver).")
        return

    athlete_id = _athlete_id()
    print(f"Athlete intervals.icu : {athlete_id}")

    aujourd_hui = dt.datetime.now(PARIS).date()
    plus_ancien = aujourd_hui - dt.timedelta(days=JOURS_FENETRE - 1)
    wellness = _get(f"/athlete/{athlete_id}/wellness", {
        "oldest": plus_ancien.isoformat(),
        "newest": aujourd_hui.isoformat(),
    })
    if not isinstance(wellness, list) or not wellness:
        # Réponse inattendue : l'afficher (tronquée) pour diagnostiquer
        print("RÉPONSE INATTENDUE de l'API wellness — débogage :")
        print(json.dumps(wellness, ensure_ascii=False)[:1500])
        raise SystemExit("ERREUR INTERVALS : réponse wellness vide ou d'un type inattendu")

    jours = [_parse_jour(w) for w in wellness if isinstance(w, dict)]
    jours = [j for j in jours if j.get("date")]
    if not jours:
        print("Aucune date reconnue dans les entrées wellness — débogage :")
        print(json.dumps(wellness[:2], ensure_ascii=False)[:1500])
        raise SystemExit("ERREUR INTERVALS : format des entrées wellness non reconnu")

    _archive_mensuelle(jours)
    _archive_activites(athlete_id, aujourd_hui)

    stats = _stats_semaine()
    if stats:
        (OUT_DIR / "week.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2))
        print(
            f"Données archivées : {len(jours)} jour(s) récupéré(s), "
            f"semaine : {stats['nb_jours_vfc']} jour(s) avec VFC, "
            f"VFC moyenne {stats['vfc_moyenne_ms']} ms"
        )
    else:
        print(f"Données archivées : {len(jours)} jour(s) — stats semaine indisponibles.")
    print(f"Jours les plus récents :")
    for j in sorted(jours, key=lambda j: j["date"])[-7:]:
        print(
            f"  {j['date']} : VFC {j.get('vfc_ms')} ms, FC repos {j.get('fc_repos')}, "
            f"sommeil {j.get('sommeil_h')} h"
        )


if __name__ == "__main__":
    main()

