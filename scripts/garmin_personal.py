#!/usr/bin/env python3
"""Archive quotidienne des données Garmin Connect via la Personal API (lancée le 21/10/2026).

intervals.icu reste LA source principale du projet (VFC, CTL/ATL/TSB, activités
du coach) — ce script COMPLÈTE l'archive avec ce que l'API intervals.icu
n'expose pas : Body Battery, stress, SpO2, pas, hydratation, âge fitness, et
surtout les fichiers .FIT originaux des activités → courbes de puissance
(records 5 s / 1 min / 5 min / 20 min absents de summaries/monthly/records.json).

Clé API personnelle : créée par Cyril sur Garmin Connect web (page « Personal
API Key », expiration 30/90/180 jours ou 1 an — email de rappel ~1 semaine
avant l'expiration), stockée dans le secret GitHub GARMIN_PERSONAL_API_KEY.
Authentification en deux temps (pas de refresh token, le token JWT est valable
24 h — on en redemande un à chaque exécution, pattern adapté à un job
quotidien) :
  1. POST /personalapitoken-service/personal-api/personal-api-key/token avec
     l'en-tête « Authorization: ApiKey <clé> » → JWT (corps texte brut) ;
  2. endpoints de données avec « Authorization: Bearer <JWT> ».
Endpoints documentés (Swagger Personal API + client officiel
garmin/connect-personal-api-python), base https://personal.connectapi.garmin.com.

Archives écrites (cumulatives, rien n'est jamais supprimé, re-exécutable) :
- summaries/garmin/wellness/<YYYY>-MM.json :
    plages   = réponses brutes des endpoints à plage de dates (stress, pas,
               hydratation, body battery, résumés quotidiens, VFC, sommeil,
               poids, activités wellness) du mois courant, rafraîchies à
               chaque run (du 1er au jour même) ; le mois précédent est
               re-rafraîchi les 2 premiers jours du mois (données de fin de
               mois complètes) ;
    par_jour = endpoints à date unique (sommeil détaillé, VFC, SpO2, âge
               fitness, FC, alimentation) pour hier et aujourd'hui, fusionnés
               par date. Rattrapage automatique des mois manquants
               (GARMIN_JOURS_BACKFILL, défaut 365, 0 = désactivé — plages
               uniquement, les endpoints à date unique seraient trop nombreux).
- summaries/garmin/activities/<YYYY>-MM.json : activités (endpoint de
    recherche, fusion par activityId) — archive cumulée illimitée ; fenêtre
    hier→aujourd'hui + mois complet + rattrapage des mois manquants (365 j).
- summaries/garmin/power_curves/<YYYY>-MM.json : courbes de puissance par
    activité (moyenne max sur 1 s / 5 s / 1 min / 5 min / 20 min), calculées
    depuis le .FIT original téléchargé à la demande. Les .FIT bruts ne sont
    PAS committés (trop volumineux pour git) : seules les courbes le sont.
- summaries/garmin/fit_traites.json : état .FIT par activité (ok /
    sans_puissance / sans_fit ; les téléchargements en échec sont réessayés,
    3 tentatives max avant abandon).
- summaries/garmin/records.json : records de puissance (1 s, 5 s, 1 min,
    5 min, 20 min), recalculés sur TOUTES les courbes archivées. Ce fichier
    est INDÉPENDANT de summaries/monthly/records.json (intervals.icu), qui
    n'est ni renommé ni écrasé.
- summaries/garmin/profil.json : zones FC + profil (rafraîchis chaque run).
- summaries/garmin/dernier_run.txt : marqueur anti-double-run (en été les
    DEUX crons tombent dans le créneau ; un second run du jour s'arrête sans
    rien refaire — GARMIN_FORCE_RUN=1 court-circuite créneau ET marqueur).

Exécuté par GitHub Actions (garmin-daily.yml) dans le créneau 12h00-14h00 à
Paris — créneau DISTINCT du pipeline intervals (7h00-9h00) pour éviter toute
collision de commit/push entre les deux workflows.
- Secret absent → message clair, sortie SANS erreur (installation en attente).
- Clé refusée (401) → erreur (run rouge visible sur la page Actions) :
  régénérer la clé sur Garmin Connect puis mettre à jour le secret.
"""
import datetime as dt
import io
import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zoneinfo

BASE = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = BASE / "summaries" / "garmin"
WELLNESS_DIR = OUT_DIR / "wellness"
ACTIVITES_DIR = OUT_DIR / "activities"
COURBES_DIR = OUT_DIR / "power_curves"
PARIS = zoneinfo.ZoneInfo("Europe/Paris")
API = "https://personal.connectapi.garmin.com"
TOKEN_URL = API + "/personalapitoken-service/personal-api/personal-api-key/token"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
# Créneau d'exécution à Paris : 12h30 planifié (double cron été/hiver), fenêtre
# de deux heures pour absorber le retard systématique du cron GitHub.
CRENEAU_DEBUT = dt.time(12, 0)
CRENEAU_FIN = dt.time(14, 0)
TENTATIVES_MAX = 3   # téléchargements .FIT en échec avant abandon définitif
DUREES_COURBE = [1, 5, 60, 300, 1200]  # secondes
LIBELLES_COURBE = {1: "1s", 5: "5s", 60: "1min", 300: "5min", 1200: "20min"}
PAUSE = 0.3          # pause (s) entre requêtes — API personnelle, pas de limite documentée

# Endpoints à plage de dates (réponses brutes archivées telles quelles dans
# la clé « plages » du fichier mensuel wellness).
ENDPOINTS_PLAGE = {
    "stress": "/usersummary-service/stats/stress/daily/{d1}/{d2}",
    "pas": "/usersummary-service/stats/steps/daily/{d1}/{d2}",
    "hydratation": "/usersummary-service/stats/hydration/daily/{d1}/{d2}",
    "body_battery": "/wellness-service/wellness/bodyBattery/reports/daily?startDate={d1}&endDate={d2}",
    "resumes_quotidiens": "/usersummary-service/v2/dailysummaries?startDate={d1}&endDate={d2}",
    "vfc_resumes": "/hrv-service/hrv/daily/{d1}/{d2}",
    "sommeil_resumes": "/sleep-service/sleep/dailySleepSummariesByDate?startDate={d1}&endDate={d2}",
    "poids": "/weight-service/weight/range/{d1}/{d2}?includeAll=true",
    "activites_wellness": "/wellnessactivity-service/activity/summary/range/{d1}/{d2}",
}

# Endpoints à date unique (archivés dans la clé « par_jour », pour hier et
# aujourd'hui uniquement — trop nombreux pour un rattrapage historique).
ENDPOINTS_JOUR = {
    "sommeil": "/sleep-service/sleep/dailySleep?date={d}",
    "vfc": "/hrv-service/hrv/{d}",
    "spo2": "/wellness-service/wellness/daily/spo2acclimation/{d}",
    "age_fitness": "/fitnessage-service/fitnessage/v2/{d}",
    "fc": "/wellness-service/wellness/dailyHeartRate?date={d}",
    "alimentation": "/nutrition-service/food/logs/{d}",
}


def _env_int(nom: str, defaut: int) -> int:
    try:
        return int(os.environ.get(nom, str(defaut)))
    except (TypeError, ValueError):
        return defaut


JOURS_BACKFILL = _env_int("GARMIN_JOURS_BACKFILL", 365)  # horizon de rattrapage (0 = désactivé)
FIT_MAX = _env_int("GARMIN_FIT_MAX", 60)  # nb max de .FIT traités par run


def _creneau_valide() -> bool:
    """Vrai s'il est entre 12h00 et 14h00 à Paris (ajustement été/hiver :
    GitHub déclenche aux deux créneaux UTC possibles, la fenêtre de deux
    heures absorbe le retard du cron ; en été les deux crons tombent dedans —
    le marqueur anti-double-run gère ce cas)."""
    if os.environ.get("GARMIN_FORCE_RUN"):
        return True  # déclenchement manuel : toujours exécuter
    return CRENEAU_DEBUT <= dt.datetime.now(PARIS).time() < CRENEAU_FIN


def _jeton() -> str:
    """Échange la clé personnelle contre un token JWT (valable 24 h, pas de
    refresh token — cf. FAQ officielle de la Personal API)."""
    cle = os.environ.get("GARMIN_PERSONAL_API_KEY", "").strip()
    if not cle:
        raise SystemExit("ERREUR GARMIN : secret GARMIN_PERSONAL_API_KEY absent")
    req = urllib.request.Request(TOKEN_URL, method="POST", headers={
        "Authorization": "ApiKey " + cle,
        "Accept": "*/*",
        "User-Agent": UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as rep:
            jeton = rep.read().decode().strip()
    except urllib.error.HTTPError as e:
        corps = e.read().decode(errors="replace")[:200]
        if e.code == 401:
            raise SystemExit(
                "ERREUR GARMIN : clé personnelle refusée (401). Régénérer la clé sur "
                "Garmin Connect (page « Personal API Key ») puis mettre à jour le "
                "secret GARMIN_PERSONAL_API_KEY du dépôt."
            )
        raise SystemExit(f"ERREUR GARMIN {e.code} (obtention du token) : {corps}")
    if not jeton:
        raise SystemExit("ERREUR GARMIN : réponse du service de token vide")
    return jeton


def _get(jeton: str, chemin: str, params: dict | None = None):
    """GET authentifié (Bearer) — retourne le JSON décodé, None si corps vide."""
    url = API + chemin
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + jeton,
        "Accept": "application/json",
        "User-Agent": UA,
    }, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as rep:
            corps = rep.read()
            if not corps:
                return None
            return json.loads(corps.decode())
    except urllib.error.HTTPError as e:
        extrait = e.read().decode(errors="replace")[:300]
        raise SystemExit(f"ERREUR GARMIN {e.code} sur {chemin} : {extrait}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"ERREUR GARMIN : réponse non JSON sur {chemin} : {e}")


def _lire_json(chemin: pathlib.Path, defaut=None):
    try:
        return json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaut


def _ecrire_json(chemin: pathlib.Path, donnees) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(json.dumps(donnees, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _date_activite(a: dict) -> str | None:
    """Date locale d'une activité Garmin : startTimeLocal (ex. « 2026-11-08
    09:12:33.0 »), sinon startTimeGMT/startTime. Retourne YYYY-MM-DD ou None."""
    for cle in ("startTimeLocal", "startTimeGMT", "startTime"):
        v = a.get(cle)
        if isinstance(v, str) and len(v) >= 10 and v[:4].isdigit():
            return v[:10]
    return None


def _sport_activite(a: dict) -> str:
    """Type d'activité : activityTypeDTO.typeKey (ex. « cycling », « running »),
    sinon activityType.typeKey, sinon « autre »."""
    for cle in ("activityTypeDTO", "activityType"):
        v = a.get(cle)
        if isinstance(v, dict):
            cle2 = v.get("typeKey")
            if cle2:
                return str(cle2)
        elif isinstance(v, str) and v:
            return v
    return "autre"


# ---------------------------------------------------------------------------
# Wellness
# ---------------------------------------------------------------------------

def _plages(jeton: str, d1: str, d2: str) -> dict:
    """Récupère tous les endpoints à plage de dates (best effort par endpoint :
    un échec n'interrompt pas les autres, la plage est simplement vide)."""
    plages = {}
    for nom, modele in ENDPOINTS_PLAGE.items():
        try:
            reponse = _get(jeton, modele.format(d1=d1, d2=d2))
            plages[nom] = reponse if reponse is not None else []
            time.sleep(PAUSE)
        except SystemExit as e:
            print(f"⚠️ Plage « {nom} » non récupérée ({d1} → {d2}) : {e}")
            plages[nom] = []
    return plages


def _journees(jeton: str, dates: list[str]) -> dict[str, dict]:
    """Récupère les endpoints à date unique pour chaque date donnée (best
    effort par endpoint)."""
    par_date = {}
    for d in dates:
        entrees = {}
        for nom, modele in ENDPOINTS_JOUR.items():
            try:
                entrees[nom] = _get(jeton, modele.format(d=d))
                time.sleep(PAUSE)
            except SystemExit as e:
                print(f"⚠️ Donnée « {nom} » du {d} non récupérée : {e}")
        if entrees:
            par_date[d] = entrees
    return par_date


def _ecrire_mois_wellness(mois: str, plages: dict | None = None, par_jour: dict | None = None) -> None:
    """Fusionne dans summaries/garmin/wellness/<YYYY>-MM.json. Re-exécutable :
    chaque plage/écrase sa clé, chaque jour écrase sa date."""
    chemin = WELLNESS_DIR / (mois + ".json")
    data = _lire_json(chemin, {})
    if not isinstance(data, dict):
        data = {}
    if plages is not None:
        existantes = data.get("plages") or {}
        if not isinstance(existantes, dict):
            existantes = {}
        existantes.update(plages)
        data["plages"] = existantes
    if par_jour is not None:
        existant_pj = data.get("par_jour") or {}
        if not isinstance(existant_pj, dict):
            existant_pj = {}
        for date, entrees in par_jour.items():
            jour = existant_pj.get(date) or {}
            if not isinstance(jour, dict):
                jour = {}
            jour.update(entrees)
            existant_pj[date] = jour
        data["par_jour"] = existant_pj
    _ecrire_json(chemin, data)


def _rattraper_wellness(jeton: str, aujourd_hui) -> int:
    """Récupère les mois wellness manquants de l'horizon de rattrapage
    (GARMIN_JOURS_BACKFILL jours) — plages uniquement. Un mois sans donnée
    reçoit quand même son fichier (plages vides) pour ne pas être redemandé
    chaque jour ; un mois en échec réseau complet est réessayé au run
    suivant."""
    if JOURS_BACKFILL <= 0:
        return 0
    debut = aujourd_hui - dt.timedelta(days=JOURS_BACKFILL - 1)
    limite = aujourd_hui.strftime("%Y-%m")
    curseur = dt.date(debut.year, debut.month, 1)
    faits = 0
    while curseur.strftime("%Y-%m") < limite:
        libelle = curseur.strftime("%Y-%m")
        if curseur.month == 12:
            fin = dt.date(curseur.year, 12, 31)
            suivant = dt.date(curseur.year + 1, 1, 1)
        else:
            fin = dt.date(curseur.year, curseur.month + 1, 1) - dt.timedelta(days=1)
            suivant = dt.date(curseur.year, curseur.month + 1, 1)
        chemin = WELLNESS_DIR / (libelle + ".json")
        if not chemin.exists():
            plages = _plages(jeton, curseur.isoformat(), fin.isoformat())
            _ecrire_mois_wellness(libelle, plages=plages)
            vides = all(not v for v in plages.values())
            print(f"  wellness {libelle} : rattrapé" + (" (aucune donnée)" if vides else ""))
            faits += 1
        curseur = suivant
    if faits:
        print(f"Rattrapage wellness : {faits} mois récupéré(s) (horizon {JOURS_BACKFILL} jours).")
    return faits


# ---------------------------------------------------------------------------
# Activités
# ---------------------------------------------------------------------------

def _activites_plage(jeton: str, d1: str, d2: str) -> list[dict]:
    """Recherche des activités d'une plage de dates (pagination : 20 par page,
    garde-fou 20 pages). La première page en échec propage l'erreur, les pages
    suivantes en échec sont juste ignorées."""
    toutes: list[dict] = []
    position = 0
    for _ in range(20):
        page = _get(
            jeton,
            "/activitylist-service/activities/search/activities",
            {"start": position, "limit": 20, "startDate": d1, "endDate": d2},
        )
        if not isinstance(page, list):
            if position == 0:
                raise SystemExit(f"ERREUR GARMIN : réponse activités inattendue ({d1} → {d2})")
            print("⚠️ Page d'activités suivante ignorée (réponse inattendue)")
            break
        toutes.extend(a for a in page if isinstance(a, dict))
        if len(page) < 20:
            break
        position += 20
        time.sleep(PAUSE)
    return toutes


def _fusionner_activites(activites: list[dict]) -> int:
    """Fusionne des activités dans summaries/garmin/activities/<YYYY>-MM.json,
    regroupées par mois de leur date locale, fusion par activityId (une
    activité modifiée écrase son ancienne version, jamais dupliquée)."""
    ACTIVITES_DIR.mkdir(parents=True, exist_ok=True)
    par_mois: dict[str, dict[str, dict]] = {}
    sans_date = 0
    for a in activites:
        date = _date_activite(a)
        if not date:
            sans_date += 1
            continue
        identifiant = a.get("activityId")
        if identifiant in (None, ""):
            continue
        par_mois.setdefault(date[:7], {})[str(identifiant)] = a
    for mois, par_id in par_mois.items():
        chemin = ACTIVITES_DIR / (mois + ".json")
        existant = _lire_json(chemin, {})
        if not isinstance(existant, dict):
            existant = {}
        existant.update(par_id)
        _ecrire_json(chemin, existant)
    if sans_date:
        print(f"⚠️ {sans_date} activité(s) sans date lisible — non fusionnée(s)")
    return len(activites) - sans_date


def _rattraper_activites(jeton: str, aujourd_hui) -> int:
    """Récupère les mois d'activités manquants de l'horizon de rattrapage
    (best effort : un mois en échec est réessayé au run suivant ; un mois sans
    activité reçoit un fichier vide pour ne pas être redemandé)."""
    if JOURS_BACKFILL <= 0:
        return 0
    debut = aujourd_hui - dt.timedelta(days=JOURS_BACKFILL - 1)
    limite = aujourd_hui.strftime("%Y-%m")
    curseur = dt.date(debut.year, debut.month, 1)
    faits = 0
    while curseur.strftime("%Y-%m") < limite:
        libelle = curseur.strftime("%Y-%m")
        if curseur.month == 12:
            fin = dt.date(curseur.year, 12, 31)
            suivant = dt.date(curseur.year + 1, 1, 1)
        else:
            fin = dt.date(curseur.year, curseur.month + 1, 1) - dt.timedelta(days=1)
            suivant = dt.date(curseur.year, curseur.month + 1, 1)
        chemin = ACTIVITES_DIR / (libelle + ".json")
        if not chemin.exists():
            try:
                acts = _activites_plage(jeton, curseur.isoformat(), fin.isoformat())
            except SystemExit as e:
                print(f"⚠️ Activités {libelle} non récupérées (réessayé au prochain run) : {e}")
                curseur = suivant
                continue
            _fusionner_activites(acts)
            if not acts:
                _ecrire_json(chemin, {})
            print(f"  activités {libelle} : {len(acts)} archivée(s)")
            faits += 1
        curseur = suivant
    if faits:
        print(f"Rattrapage activités : {faits} mois récupéré(s) (horizon {JOURS_BACKFILL} jours).")
    return faits


def _archiver_activites(jeton: str, aujourd_hui, hier) -> bool:
    """Activités : fenêtre hier→aujourd'hui (couvre la frontière de mois) +
    mois complet courant + mois précédent les 3 premiers jours du mois
    (rafraîchit les activités de fin de mois enregistrées le soir) +
    rattrapage des mois manquants. Retourne Vrai si la fenêtre courante a
    fonctionné (sinon le rattrapage est reporté — probable erreur systémique)."""
    total = 0
    ok = False
    try:
        acts = _activites_plage(jeton, hier.isoformat(), aujourd_hui.isoformat())
        total += _fusionner_activites(acts)
        ok = True
    except SystemExit as e:
        print(f"⚠️ Activités hier→aujourd'hui non récupérées : {e}")
    debut_mois = dt.date(aujourd_hui.year, aujourd_hui.month, 1)
    try:
        acts = _activites_plage(jeton, debut_mois.isoformat(), aujourd_hui.isoformat())
        total += _fusionner_activites(acts)
        ok = True
    except SystemExit as e:
        print(f"⚠️ Activités du mois courant non récupérées : {e}")
    if aujourd_hui.day <= 3:
        fin_prec = debut_mois - dt.timedelta(days=1)
        debut_prec = dt.date(fin_prec.year, fin_prec.month, 1)
        try:
            acts = _activites_plage(jeton, debut_prec.isoformat(), fin_prec.isoformat())
            total += _fusionner_activites(acts)
            print(f"Mois précédent rafraîchi : {len(acts)} activité(s)")
        except SystemExit as e:
            print(f"⚠️ Mois précédent d'activités non rafraîchi : {e}")
    print(f"Activités : {total} fusionnée(s) (fenêtre hier→aujourd'hui + mois courant)")
    if ok:
        _rattraper_activites(jeton, aujourd_hui)
    else:
        print("⚠️ Fenêtre courante en échec — rattrapage activités reporté au prochain run.")
    return ok


# ---------------------------------------------------------------------------
# Fichiers .FIT et courbes de puissance
# ---------------------------------------------------------------------------

def _telecharger_zip(jeton: str, identifiant: str) -> bytes:
    """Télécharge le fichier original d'une activité (ZIP contenant le .FIT)."""
    url = f"{API}/download-service/files/activity/{identifiant}"
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + jeton,
        "Accept": "application/zip, application/octet-stream",
        "User-Agent": UA,
    }, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as rep:
            contenu = rep.read()
    except urllib.error.HTTPError as e:
        extrait = e.read().decode(errors="replace")[:200]
        raise SystemExit(f"ERREUR GARMIN {e.code} (téléchargement {identifiant}) : {extrait}")
    if not contenu:
        raise SystemExit(f"ERREUR GARMIN : téléchargement {identifiant} vide")
    return contenu


def _extraire_fit(contenu: bytes):
    """Extrait le .FIT principal du ZIP téléchargé (le plus grand .fit si
    plusieurs), ou None s'il n'y en a pas."""
    try:
        with zipfile.ZipFile(io.BytesIO(contenu)) as archive:
            noms = [n for n in archive.namelist() if n.lower().endswith(".fit")]
            if not noms:
                return None
            principal = max(noms, key=lambda n: archive.getinfo(n).file_size)
            return archive.read(principal)
    except (zipfile.BadZipFile, OSError, KeyError) as e:
        raise SystemExit(f"ZIP .FIT illisible : {e}")


def _max_moyenne(grille: list[int], fenetre: int):
    """Moyenne max sur une fenêtre glissante de `fenetre` secondes (grille à
    1 s). None si l'activité est plus courte que la fenêtre."""
    if fenetre > len(grille):
        return None
    somme = sum(grille[:fenetre])
    meilleur = somme
    for i in range(fenetre, len(grille)):
        somme += grille[i] - grille[i - fenetre]
        if somme > meilleur:
            meilleur = somme
    return round(meilleur / fenetre, 1)


def _courbe_puissance(fit_bytes: bytes):
    """Parse le .FIT et calcule la courbe de puissance (moyennes max 1 s / 5 s /
    1 min / 5 min / 20 min). Retourne (courbe | None, erreur | None) :
    - courbe None + erreur None = pas de puissance exploitable (ex. course à
      pied sans capteur) → état « sans_puissance » ;
    - courbe None + erreur = .FIT illisible → nouvelle tentative plus tard."""
    try:
        import fitparse
    except ImportError:
        return None, "fitparse non installé"
    try:
        fichier = fitparse.FitFile(
            io.BytesIO(fit_bytes), data_processor=fitparse.StandardUnitsDataProcessor()
        )
        echantillons = []
        for message in fichier.get_messages("record"):
            valeurs = message.get_values()
            puissance = valeurs.get("power")
            horodatage = valeurs.get("timestamp")
            if puissance is None or horodatage is None:
                continue
            if not isinstance(puissance, (int, float)):
                continue
            try:
                secondes = int(horodatage.timestamp())
            except (AttributeError, OSError, ValueError, TypeError):
                continue
            echantillons.append((secondes, int(puissance)))
    except Exception as e:  # fitparse lève des exceptions variées sur fichiers inattendus
        return None, f"erreur de lecture .FIT : {e}"
    if len(echantillons) < 30:
        return None, None  # pas assez de mesures de puissance
    echantillons.sort()
    t0 = echantillons[0][0]
    t1 = echantillons[-1][0]
    if t1 - t0 + 1 > 12 * 86400:  # garde-fou : au-delà de 12 h on tronque
        echantillons = [e for e in echantillons if e[0] - t0 < 12 * 86400]
        t1 = echantillons[-1][0]
    duree = t1 - t0 + 1
    # Grille à 1 seconde : forward-fill, un trou > 3 s vaut 0 W (repos)
    grille = [0] * duree
    index = 0
    valeur = 0
    for i in range(duree):
        t = t0 + i
        while index < len(echantillons) and echantillons[index][0] <= t:
            valeur = echantillons[index][1]
            index += 1
        if index and t - echantillons[index - 1][0] <= 3:
            grille[i] = valeur
    watts = {}
    for fenetre in DUREES_COURBE:
        watts[str(fenetre)] = _max_moyenne(grille, fenetre)
    return {"duree_s": duree, "nb_echantillons": len(echantillons), "watts": watts}, None


def _traiter_fits(jeton: str, aujourd_hui) -> int:
    """Télécharge et calcule les courbes de puissance des activités non
    traitées (les plus récentes d'abord), dans la limite de FIT_MAX par run —
    le rattrapage complet se fait donc en quelques jours, ou en un run manuel
    avec GARMIN_FIT_MAX élevé. Les échecs sont réessayés (3 tentatives max)."""
    if not ACTIVITES_DIR.exists():
        return 0
    try:
        import fitparse  # noqa: F401
    except ImportError:
        print("⚠️ fitparse non installé — courbes de puissance non calculées ce run.")
        return 0
    limite_mois = (aujourd_hui - dt.timedelta(days=JOURS_BACKFILL)).strftime("%Y-%m")
    etats = _lire_json(OUT_DIR / "fit_traites.json", {})
    if not isinstance(etats, dict):
        etats = {}
    candidates: list[dict] = []
    for chemin in sorted(ACTIVITES_DIR.glob("*.json"), reverse=True):
        if chemin.stem < limite_mois:
            break  # triés du plus récent au plus ancien : on s'arrête
        data = _lire_json(chemin, {})
        if not isinstance(data, dict):
            continue
        for identifiant, a in data.items():
            etat = etats.get(str(identifiant)) or {}
            if etat.get("etat") in ("ok", "sans_puissance", "sans_fit", "abandonne"):
                continue
            if int(etat.get("tentatives") or 0) >= TENTATIVES_MAX:
                etats[str(identifiant)] = {
                    "etat": "abandonne",
                    "tentatives": int(etat.get("tentatives") or 0),
                }
                continue
            if not isinstance(a, dict):
                continue
            horodatage = str(a.get("startTimeLocal") or a.get("startTimeGMT") or "")
            candidates.append((horodatage, a))
    candidates.sort(key=lambda c: c[0], reverse=True)
    candidates = [a for _, a in candidates]
    if not candidates:
        _ecrire_json(OUT_DIR / "fit_traites.json", etats)
        return 0
    nb = min(len(candidates), FIT_MAX)
    print(f"Fichiers .FIT : {len(candidates)} en attente, {nb} traité(s) ce run (max {FIT_MAX}).")
    courbes = 0
    for a in candidates[:nb]:
        identifiant = str(a.get("activityId") or "")
        if not identifiant:
            continue
        etat = etats.get(identifiant) or {}
        tentatives = int(etat.get("tentatives") or 0) + 1
        try:
            contenu = _telecharger_zip(jeton, identifiant)
            fit = _extraire_fit(contenu)
        except SystemExit as e:
            print(f"  ⚠️ .FIT {identifiant} non traité (tentative {tentatives}) : {e}")
            etats[identifiant] = {"etat": "echec", "tentatives": tentatives}
            continue
        if not fit:
            etats[identifiant] = {"etat": "sans_fit", "tentatives": tentatives}
            continue
        courbe, erreur = _courbe_puissance(fit)
        if erreur:
            print(f"  ⚠️ .FIT {identifiant} illisible (tentative {tentatives}) : {erreur}")
            etats[identifiant] = {"etat": "echec", "tentatives": tentatives}
            continue
        if not courbe:
            etats[identifiant] = {"etat": "sans_puissance", "tentatives": tentatives}
            continue
        date = _date_activite(a) or aujourd_hui.isoformat()
        entree = {
            "date": date,
            "sport": _sport_activite(a),
            "nom": a.get("activityName"),
            "duree_s": courbe["duree_s"],
            "nb_echantillons": courbe["nb_echantillons"],
            "courbe_watts": courbe["watts"],
        }
        chemin_courbes = COURBES_DIR / (date[:7] + ".json")
        mois_courbes = _lire_json(chemin_courbes, {})
        if not isinstance(mois_courbes, dict):
            mois_courbes = {}
        mois_courbes[identifiant] = entree
        _ecrire_json(chemin_courbes, mois_courbes)
        etats[identifiant] = {"etat": "ok", "tentatives": tentatives}
        courbes += 1
        print(
            f"  .FIT {identifiant} ({date}, {entree['sport']}) : "
            f"5 s {entree['courbe_watts'].get('5')} W, "
            f"1 min {entree['courbe_watts'].get('60')} W, "
            f"5 min {entree['courbe_watts'].get('300')} W"
        )
        time.sleep(PAUSE)
    _ecrire_json(OUT_DIR / "fit_traites.json", etats)
    print(f"Courbes de puissance : {courbes} nouvelle(s) ce run.")
    return courbes


def _calculer_records() -> dict:
    """Recalcule les records de puissance (toutes durées) sur l'ensemble des
    courbes archivées dans summaries/garmin/power_curves/."""
    records = {libelle: None for libelle in LIBELLES_COURBE.values()}
    if not COURBES_DIR.exists():
        return records
    for chemin in sorted(COURBES_DIR.glob("*.json")):
        mois = _lire_json(chemin, {})
        if not isinstance(mois, dict):
            continue
        for identifiant, entree in mois.items():
            if not isinstance(entree, dict):
                continue
            courbe = entree.get("courbe_watts") or {}
            for duree, watts in courbe.items():
                if watts is None:
                    continue
                try:
                    libelle = LIBELLES_COURBE[int(duree)]
                except (KeyError, ValueError):
                    continue
                actuel = records[libelle]
                if actuel is None or watts > actuel["watts"]:
                    records[libelle] = {
                        "watts": watts,
                        "date": entree.get("date"),
                        "sport": entree.get("sport"),
                        "nom": entree.get("nom"),
                        "activite": str(identifiant),
                    }
    return records


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not _creneau_valide():
        print("Hors créneau 12h00-14h00 à Paris — rien à faire (ajustement heure été/hiver).")
        return

    aujourd_hui = dt.datetime.now(PARIS).date()
    marqueur = OUT_DIR / "dernier_run.txt"
    if not os.environ.get("GARMIN_FORCE_RUN") and marqueur.exists():
        try:
            deja_fait = marqueur.read_text(encoding="utf-8").strip()
        except OSError:
            deja_fait = ""
        if deja_fait == aujourd_hui.isoformat():
            print("Archive Garmin déjà exécutée aujourd'hui — rien à faire (anti-double-run).")
            return

    cle = os.environ.get("GARMIN_PERSONAL_API_KEY", "").strip()
    if not cle:
        print(
            "⚠️ GARMIN : secret GARMIN_PERSONAL_API_KEY absent. Créer la clé sur Garmin "
            "Connect web (page « Personal API Key », expiration 1 an), l'ajouter aux "
            "secrets du dépôt, puis relancer ce workflow. Rien à faire en attendant."
        )
        return

    jeton = _jeton()
    print("Token Garmin obtenu (valable 24 h).")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    hier = aujourd_hui - dt.timedelta(days=1)

    # 1. Profil et zones FC — best effort, écrasés à chaque run
    try:
        zones = _get(jeton, "/biometric-service/heartRateZones/v2")
        time.sleep(PAUSE)
        profil = _get(jeton, "/userprofile-service/userprofile/settings")
        _ecrire_json(OUT_DIR / "profil.json", {
            "genere_le": dt.datetime.now(dt.timezone.utc).isoformat(),
            "zones_fc": zones,
            "profil": profil,
        })
        print("Profil et zones FC archivés.")
    except SystemExit as e:
        print(f"⚠️ Profil non archivé : {e}")

    # 2. Wellness du mois courant : plages (1er → aujourd'hui) + par_jour
    #    (hier + aujourd'hui, données de fin de mois complétées les jours 1-2)
    debut_mois = dt.date(aujourd_hui.year, aujourd_hui.month, 1)
    plages = _plages(jeton, debut_mois.isoformat(), aujourd_hui.isoformat())
    courant_ok = any(plages.values())
    _ecrire_mois_wellness(aujourd_hui.strftime("%Y-%m"), plages=plages)
    journees = _journees(jeton, [hier.isoformat(), aujourd_hui.isoformat()])
    for d, entrees in journees.items():
        _ecrire_mois_wellness(d[:7], par_jour={d: entrees})
    if aujourd_hui.day <= 2:
        fin_prec = debut_mois - dt.timedelta(days=1)
        debut_prec = dt.date(fin_prec.year, fin_prec.month, 1)
        plages_prec = _plages(jeton, debut_prec.isoformat(), fin_prec.isoformat())
        _ecrire_mois_wellness(debut_prec.strftime("%Y-%m"), plages=plages_prec)
        print("Mois précédent wellness re-rafraîchi (données de fin de mois complètes).")
    print(f"Wellness du mois courant archivé ({len(journees)} jour(s) en détail).")

    # 3. Rattrapage wellness (uniquement si le mois courant répond — sinon
    #    erreur probablement systémique, on ne pollue pas l'historique)
    if courant_ok:
        _rattraper_wellness(jeton, aujourd_hui)
    else:
        print("⚠️ Plages du mois courant vides — rattrapage wellness reporté au prochain run.")

    # 4. Activités (fenêtre + mois courant + rattrapage)
    _archiver_activites(jeton, aujourd_hui, hier)

    # 5. Courbes de puissance depuis les .FIT originaux
    _traiter_fits(jeton, aujourd_hui)

    # 6. Records de puissance, recalculés sur toutes les courbes
    records = _calculer_records()
    _ecrire_json(OUT_DIR / "records.json", {
        "genere_le": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "Personal API Garmin — courbes calculées depuis les .FIT originaux "
                  "(complément de summaries/monthly/records.json, intervals.icu)",
        "records_puissance": records,
    })
    for libelle in ("1s", "5s", "1min", "5min", "20min"):
        r = records.get(libelle)
        if r:
            print(f"Record {libelle} : {r['watts']} W ({r['date']}, {r['sport']})")

    # 7. Marqueur anti-double-run (seulement après un run complet)
    marqueur.write_text(aujourd_hui.isoformat(), encoding="utf-8")
    print("Archive Garmin terminée.")


if __name__ == "__main__":
    main()
