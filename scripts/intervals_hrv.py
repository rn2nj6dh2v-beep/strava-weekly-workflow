#!/usr/bin/env python3
"""Récupération quotidienne des données wellness intervals.icu (VFC/HRV, sommeil, etc.)

- Authentification Basic : nom d'utilisateur littéral "API_KEY",
  mot de passe = clé API (secret INTERVALS_API_KEY)
- Identifiant athlete : config/intervals.json (ou "0" = soi-même)
- Archivage mensuel cumulé : summaries/intervals/<YYYY>-MM.json (fusion par date)
- summaries/intervals/week.json : statistiques des 7 derniers jours, lues par
  scripts/strava_summary.py pour enrichir le résumé hebdomadaire du dimanche.
- Exécuté par GitHub Actions vers 8h heure de Paris (double cron été/hiver,
  le script vérifie l'heure réelle à Paris).
- INTERVALS_BACKFILL_DAYS (défaut 3) : fenêtre de récupération — permet le
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
CONFIG_PATH = BASE / "config" / "intervals.json"
API = "https://intervals.icu/api/v1"
JOURS_FENETRE = int(os.environ.get("INTERVALS_BACKFILL_DAYS", "3"))  # jours récupérés (rattrapage possible)
JOURS_SEMAINE = 7  # fenêtre des stats hebdo écrites dans week.json
PARIS = zoneinfo.ZoneInfo("Europe/Paris")
HEURE_EXECUTION = 8  # heure de Paris


def _heure_paris_valide() -> bool:
    """Vrai s'il est HEURE_EXECUTION à Paris (ajustement été/hiver : GitHub
    déclenche aux deux créneaux UTC possibles, on n'exécute que le bon)."""
    if os.environ.get("INTERVALS_FORCE_RUN"):
        return True  # déclenchement manuel : toujours exécuter
    return dt.datetime.now(PARIS).hour == HEURE_EXECUTION


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
        print(f"Pas {HEURE_EXECUTION}h à Paris — rien à faire (ajustement heure été/hiver).")
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
