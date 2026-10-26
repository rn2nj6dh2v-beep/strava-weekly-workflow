#!/usr/bin/env python3
"""Bulletin mensuel intervals.icu — exécuté par GitHub Actions le 1er du mois
vers 9h (heure de Paris). Résume le mois PRÉCÉDENT (demande de Cyril du
26/10/2026 : « je prends tout » — bulletin mensuel + suivi des records).

Calculé intégralement depuis les archives locales du dépôt (aucun appel API
intervals.icu) :
- activités du mois : summaries/intervals/activities_archive/<YYYY>-MM>.json
  (archive cumulée illimitée — données brutes de l'API, fusionnées par id)
- wellness du mois : summaries/intervals/<YYYY>-MM>.json (VFC, FC repos,
  sommeil, poids, CTL/ATL/TSB jour par jour)
- comparaison : mêmes sources pour le mois M-1

Produit :
- summaries/monthly/<YYYY>-MM>.json : stats + comparaison + analyse IA
- summaries/monthly/records.json : records toutes périodes (recalculés sur
  toute l'archive cumulée à chaque exécution, max par catégorie)
- summaries/monthly/bulletin.txt : bulletin texte
- email SMTP (best effort) avec graphiques matplotlib (best effort) :
  volume par semaine et VFC du mois

INTERVALS_MOIS=YYYY-MM : résumer un autre mois ; INTERVALS_FORCE_RUN=1 :
forcer l'exécution (workflow_dispatch).
"""
import datetime as dt
import html as html_lib
import json
import os
import pathlib
import shutil
import smtplib
import ssl
import statistics
import tempfile
import urllib.error
import urllib.request
from email.message import EmailMessage
import zoneinfo

from intervals_summary import _parse_activite, _moy  # même dossier

BASE = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = BASE / "summaries"
ARCHIVE_DIR = OUT_DIR / "intervals" / "activities_archive"
WELLNESS_DIR = OUT_DIR / "intervals"
MONTHLY_DIR = OUT_DIR / "monthly"
PARIS = zoneinfo.ZoneInfo("Europe/Paris")
HEURE_EXECUTION = 9  # heure de Paris, le 1er du mois
MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
MODELES_FALLBACK = ["mistral-small-latest", "open-mistral-nemo", "ministral-3b-latest"]
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin",
           "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
VELO = {"Ride", "VirtualRide"}
COURSE = {"Run", "TrailRun"}


def _heure_paris_valide() -> bool:
    """Vrai s'il est HEURE_EXECUTION à Paris (ajustement été/hiver : GitHub
    déclenche aux deux créneaux UTC possibles, on n'exécute que le bon)."""
    if os.environ.get("INTERVALS_FORCE_RUN"):
        return True  # déclenchement manuel : toujours exécuter
    return dt.datetime.now(PARIS).hour == HEURE_EXECUTION


def _mois_a_resumer(aujourd_hui: dt.date) -> str:
    """YYYY-MM du mois à résumer : le mois précédent (le bulletin du 1er
    résume le mois écoulé), ou INTERVALS_MOIS si fourni (workflow_dispatch)."""
    override = os.environ.get("INTERVALS_MOIS", "").strip()
    if len(override) == 7 and override[:4].isdigit() and override[4] == "-":
        return override
    premier = dt.date(aujourd_hui.year, aujourd_hui.month, 1)
    return (premier - dt.timedelta(days=1)).strftime("%Y-%m")


def _mois_precedent_libelle(mois: str) -> str:
    """YYYY-MM du mois précédant le mois donné."""
    annee, m = int(mois[:4]), int(mois[5:7])
    premier = dt.date(annee, m, 1)
    return (premier - dt.timedelta(days=1)).strftime("%Y-%m")


def _libelle_mois(mois: str) -> str:
    """'2026-10' → 'octobre 2026'."""
    return f"{MOIS_FR[int(mois[5:7]) - 1]} {mois[:4]}"


def _hmm(minutes) -> str:
    h, m = divmod(int(minutes or 0), 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def _charger_activites(mois: str) -> list[dict]:
    """Activités parsées d'un mois depuis l'archive cumulée (triées par date).
    Échec si l'archive du mois est absente : le bulletin ne peut pas être
    généré sans ses données."""
    chemin = ARCHIVE_DIR / f"{mois}.json"
    if not chemin.exists():
        raise SystemExit(f"ERREUR MENSUEL : {chemin} absent — archive cumulée incomplète")
    brut = json.loads(chemin.read_text())
    parsed = [_parse_activite(a) for a in brut.values() if isinstance(a, dict)]
    parsed.sort(key=lambda p: (p["date"], p["nom"]))
    return parsed


def _charger_wellness(mois: str) -> list[dict]:
    """Jours wellness d'un mois depuis l'archive mensuelle (triés par date)."""
    chemin = WELLNESS_DIR / f"{mois}.json"
    if not chemin.exists():
        return []
    try:
        brut = json.loads(chemin.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return [brut[d] for d in sorted(brut) if isinstance(brut[d], dict)]


def _stats_sport(ps: list[dict]) -> dict:
    return {
        "sorties": len(ps),
        "km": round(sum(p["km"] for p in ps), 1),
        "minutes": sum(p["minutes"] for p in ps),
        "denivele_m": round(sum(p["denivele_m"] or 0 for p in ps), 1),
        "puissance_ponderee_moyenne_w": _moy([p.get("puissance_ponderee_w") for p in ps]),
        "fc_moyenne": _moy([p.get("fc_moyenne") for p in ps]),
    }


def _stats_mois(parsed: list[dict], jours: list[dict]) -> dict:
    velo = [p for p in parsed if p["type"] in VELO]
    course = [p for p in parsed if p["type"] in COURSE]
    autres = [p for p in parsed if p["type"] not in VELO and p["type"] not in COURSE]
    vfcs = [j["vfc_ms"] for j in jours if j.get("vfc_ms") is not None]
    ctls = [j.get("ctl") for j in jours if j.get("ctl") is not None]
    return {
        "activites": len(parsed),
        "km": round(sum(p["km"] for p in parsed), 1),
        "minutes": sum(p["minutes"] for p in parsed),
        "denivele_m": round(sum(p["denivele_m"] or 0 for p in parsed), 1),
        "velo": _stats_sport(velo),
        "course": _stats_sport(course),
        "autres": _stats_sport(autres),
        "nb_jours_vfc": len(vfcs),
        "vfc_moyenne_ms": _moy(vfcs),
        "vfc_min_ms": min(vfcs) if vfcs else None,
        "vfc_max_ms": max(vfcs) if vfcs else None,
        "fc_repos_moyenne": _moy([j.get("fc_repos") for j in jours]),
        "sommeil_moyen_h": _moy([j.get("sommeil_h") for j in jours]),
        "poids_moyen_kg": _moy([j.get("poids_kg") for j in jours], 1),
        "ctl_debut": ctls[0] if ctls else None,
        "ctl_fin": ctls[-1] if ctls else None,
        "tsb_moyen": _moy([j.get("tsb") for j in jours]),
    }


def _calculer_records() -> dict:
    """Records toutes périodes recalculés sur TOUTE l'archive cumulée (tous
    les fichiers mensuels). Maxima par catégorie, robustes aux champs absents
    (pas de capteur de puissance → pas de record puissance)."""
    records: dict = {}

    def garder(cle: str, valeur, unite: str, p: dict) -> None:
        if valeur is None or valeur == 0:
            return
        if cle not in records or valeur > records[cle]["valeur"]:
            records[cle] = {
                "valeur": round(valeur, 1) if isinstance(valeur, float) else valeur,
                "unite": unite,
                "date": p["date"],
                "activite": p["nom"],
            }

    for chemin in sorted(ARCHIVE_DIR.glob("*.json")):
        try:
            brut = json.loads(chemin.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for a in brut.values():
            if not isinstance(a, dict):
                continue
            p = _parse_activite(a)
            if p["type"] in VELO:
                garder("plus_longue_sortie_velo_km", p["km"], "km", p)
                garder("plus_gros_denivele_velo_m", p.get("denivele_m"), "m", p)
                garder("puissance_ponderee_max_w", p.get("puissance_ponderee_w"), "W", p)
                garder("vitesse_max_velo_kmh", p.get("vitesse_max_kmh"), "km/h", p)
            elif p["type"] in COURSE:
                garder("plus_longue_sortie_course_km", p["km"], "km", p)
                if p["minutes"] >= 20 and p.get("vitesse_moyenne_kmh"):
                    garder("meilleure_allure_course_kmh", p["vitesse_moyenne_kmh"], "km/h", p)
            garder("fc_max_bpm", p.get("fc_max"), "bpm", p)
    return records


def _maj_records(records_tout: dict) -> dict:
    """Écrit records.json (max par catégorie) et retourne
    {"records": ..., "nouveaux": [clés améliorées], "premiere_fois": bool}."""
    chemin = MONTHLY_DIR / "records.json"
    existant: dict = {}
    if chemin.exists():
        try:
            existant = json.loads(chemin.read_text()).get("records", {})
        except (json.JSONDecodeError, OSError):
            existant = {}
    premiere_fois = not existant
    nouveaux = []
    for cle, rec in records_tout.items():
        ancien = existant.get(cle)
        if not ancien or rec["valeur"] > ancien["valeur"]:
            nouveaux.append(cle)
        existant[cle] = rec
    contenu = {
        "genere_le": dt.datetime.now(dt.timezone.utc).isoformat(),
        "records": existant,
    }
    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)
    chemin.write_text(json.dumps(contenu, ensure_ascii=False, indent=2, sort_keys=True))
    return {"records": existant, "nouveaux": nouveaux, "premiere_fois": premiere_fois}


def _graphiques_mensuels(parsed: list[dict], jours: list[dict], dossier: pathlib.Path):
    """PNG du bulletin : volume par semaine (vélo/course) et VFC du mois.
    Retourne ([(cid, titre, chemin)], erreur_ou_None). Best effort."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        return [], f"matplotlib indisponible : {type(e).__name__} : {e}"
    graphiques = []
    problemes = []
    try:
        semaines: dict = {}
        for p in parsed:
            try:
                d = dt.date.fromisoformat(p["date"][:10])
            except ValueError:
                continue
            cle = f"S{d.isocalendar()[1]:02d}"
            semaines.setdefault(cle, {"velo": 0.0, "course": 0.0})
            if p["type"] in VELO:
                semaines[cle]["velo"] += p["km"]
            elif p["type"] in COURSE:
                semaines[cle]["course"] += p["km"]
        if semaines:
            cles = sorted(semaines)
            vs = [semaines[c]["velo"] for c in cles]
            cs = [semaines[c]["course"] for c in cles]
            fig, ax = plt.subplots(figsize=(7.2, 3.2), dpi=150)
            ax.bar(range(len(cles)), vs, width=0.6, color="#3b82f6", label="Vélo")
            ax.bar(range(len(cles)), cs, width=0.6, bottom=vs, color="#f97316", label="Course")
            ax.set_title("Volume par semaine (km)", fontsize=11, fontweight="bold", loc="left")
            ax.set_xticks(range(len(cles)))
            ax.set_xticklabels(cles, fontsize=9)
            ax.set_ylabel("km", fontsize=9)
            ax.grid(axis="y", alpha=0.3)
            ax.legend(frameon=False, fontsize=9)
            fig.savefig(dossier / "graphe_semaines.png", dpi=150,
                        bbox_inches="tight", facecolor="white")
            plt.close(fig)
            graphiques.append(("graphe_semaines", "Volume par semaine",
                               str(dossier / "graphe_semaines.png")))
    except Exception as e:
        problemes.append(f"semaines : {type(e).__name__} : {e}")
        plt.close("all")
    try:
        vj = [j for j in jours if j.get("vfc_ms") is not None]
        if len(vj) >= 3:
            pas = max(1, len(vj) // 10)
            fig, ax = plt.subplots(figsize=(7.2, 3.2), dpi=150)
            ax.plot(range(len(vj)), [j["vfc_ms"] for j in vj],
                    marker="o", color="#ef4444", linewidth=2)
            ax.set_xticks(range(0, len(vj), pas))
            ax.set_xticklabels([str(vj[i]["date"])[5:] for i in range(0, len(vj), pas)],
                               fontsize=8)
            moyenne = statistics.mean(j["vfc_ms"] for j in vj)
            ax.axhline(moyenne, color="#9ca3af", linestyle="--", linewidth=1,
                       label=f"moyenne {round(moyenne, 1)} ms")
            ax.set_title("VFC nocturne du mois (ms)", fontsize=11,
                         fontweight="bold", loc="left")
            ax.set_ylabel("ms", fontsize=9)
            ax.grid(axis="y", alpha=0.3)
            ax.legend(frameon=False, fontsize=9)
            fig.savefig(dossier / "graphe_vfc.png", dpi=150,
                        bbox_inches="tight", facecolor="white")
            plt.close(fig)
            graphiques.append(("graphe_vfc", "VFC du mois",
                               str(dossier / "graphe_vfc.png")))
    except Exception as e:
        problemes.append(f"vfc : {type(e).__name__} : {e}")
        plt.close("all")
    return graphiques, ("; ".join(problemes) if problemes else None)


def _envoi_email(sujet: str, contenu: str, graphiques: list | None = None) -> str | None:
    """Envoie le bulletin par email via SMTP (Gmail par défaut), images cid
    inline si fournies. Retourne None si succès, la raison sinon. Best effort."""
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    to = os.environ.get("MAIL_TO") or user
    if not user or not password:
        return "secrets SMTP_USER / SMTP_PASSWORD absents — email non envoyé"
    msg = EmailMessage()
    msg["Subject"] = sujet
    msg["From"] = f"Intervals Bot <{user}>"
    msg["To"] = to
    msg.set_content(contenu)
    if graphiques:
        corps_html = html_lib.escape(contenu).replace("\n", "<br>")
        blocs = "".join(
            f'<p style="margin:20px 0 4px;font-weight:bold;color:#374151;'
            f'font-size:13px;">{html_lib.escape(titre)}</p>'
            f'<img src="cid:{cid}" alt="{html_lib.escape(titre)}" '
            f'style="max-width:680px;width:100%;height:auto;'
            f'border:1px solid #e5e7eb;border-radius:8px;">'
            for cid, titre, _chemin in graphiques
        )
        html = (
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
            'color:#111827;line-height:1.5;max-width:680px;">' + corps_html
            + '<hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0;">'
            + '<p style="margin:0 0 12px;font-weight:bold;font-size:16px;">📈 Le mois en images</p>'
            + blocs + "</div>"
        )
        msg.add_alternative(html, subtype="html")
        for cid, _titre, chemin in graphiques:
            try:
                with open(chemin, "rb") as f:
                    msg.get_payload()[1].add_related(
                        f.read(), maintype="image", subtype="png", cid=f"<{cid}>")
            except Exception as e:
                return f"intégration image {cid} : {type(e).__name__} : {e}"
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(user, password)
            server.send_message(msg)
        return None
    except Exception as e:
        return f"{type(e).__name__} : {e}"


def _analyse_ia_mois(stats: dict, precedent: dict | None, nouveaux: list) -> tuple[str | None, str | None]:
    """Analyse coach du mois via l'API Mistral (modèles de secours).
    Best effort : retourne (texte, None) ou (None, raison)."""
    key = os.environ.get("MISTRAL_API_KEY")
    if not key:
        return None, "MISTRAL_API_KEY absente"
    contexte = {"mois": stats}
    if precedent:
        contexte["mois_precedent"] = {
            k: precedent.get(k) for k in (
                "activites", "km", "minutes", "denivele_m", "velo", "course",
                "vfc_moyenne_ms", "ctl_debut", "ctl_fin",
            )
        }
    prompt = (
        "Tu es un coach sportif. Analyse le mois d'entraînement ci-dessous (données "
        "intervals.icu au format JSON : volume par sport, VFC/FC de repos/sommeil/"
        "poids, forme CTL). Compare-le au mois précédent si fourni : évalue la "
        "progression réelle (volume, intensité, forme) et la récupération. "
        + (
            f"De nouveaux records ont été établis : "
            f"{', '.join(nouveaux).replace('_', ' ')}. Salue cette progression. "
            if nouveaux else ""
        )
        + "Réponds en français, 180 mots maximum : bilan du mois, progression "
        "observée, 1-2 axes concrets pour le mois suivant. Texte fluide en "
        "2 paragraphes courts, ton direct et encourageant.\n\n"
        "MOIS À ANALYSER :\n" + json.dumps(contexte, ensure_ascii=False, default=str)
    )
    derniere_erreur = None
    for modele in MODELES_FALLBACK:
        payload = json.dumps({
            "model": modele,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 450,
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
        print(f"Pas {HEURE_EXECUTION}h à Paris — rien à faire (ajustement heure été/hiver).")
        return

    aujourd_hui = dt.datetime.now(PARIS).date()
    mois = _mois_a_resumer(aujourd_hui)
    mois_prec = _mois_precedent_libelle(mois)
    print(f"Bulletin mensuel : {mois} (comparaison : {mois_prec})")

    parsed = _charger_activites(mois)
    jours = _charger_wellness(mois)
    if not parsed and not jours:
        raise SystemExit(f"ERREUR MENSUEL : aucune donnée pour {mois}")

    stats = _stats_mois(parsed, jours)

    precedent = None
    try:
        parsed_prec = _charger_activites(mois_prec)
        precedent = _stats_mois(parsed_prec, _charger_wellness(mois_prec))
    except SystemExit:
        precedent = None  # best effort : pas de comparaison si M-1 absent

    records_tout = _calculer_records()
    records = _maj_records(records_tout)
    if records["nouveaux"]:
        print(f"🏆 Records mis à jour : {', '.join(records['nouveaux'])}")

    analyse, raison_erreur = _analyse_ia_mois(stats, precedent, records["nouveaux"])

    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)
    maintenant = dt.datetime.now(dt.timezone.utc)
    result = {
        "genere_le": maintenant.isoformat(),
        "mois": mois,
        "source": "archives intervals.icu (activities_archive + wellness mensuel)",
        "stats": stats,
        "comparaison_mois_precedent": precedent,
        "records": records["records"],
        "records_nouveaux": records["nouveaux"],
    }
    if analyse:
        result["analyse_ia"] = analyse
        result["modele_ia"] = MODELES_FALLBACK[0]
    elif raison_erreur:
        result["analyse_ia_erreur"] = raison_erreur
    chemin_resultat = MONTHLY_DIR / f"{mois}.json"
    chemin_resultat.write_text(json.dumps(result, ensure_ascii=False, indent=2))

    # Bulletin texte
    lignes = [f"📊 Bulletin mensuel — {_libelle_mois(mois)}"]
    lignes.append(
        f"• {stats['activites']} activité(s), {stats['km']} km, "
        f"{_hmm(stats['minutes'])}, {stats['denivele_m']} m de D+"
    )
    for cle, emoji in (("velo", "🚴"), ("course", "🏃")):
        s = stats.get(cle) or {}
        if s.get("sorties"):
            detail = f"• {emoji} {cle.capitalize()} : {s['sorties']} sortie(s), {s['km']} km"
            if s.get("denivele_m"):
                detail += f", {s['denivele_m']} m D+"
            if s.get("puissance_ponderee_moyenne_w"):
                detail += f", {s['puissance_ponderee_moyenne_w']} W pondérés"
            lignes.append(detail)
    if stats.get("nb_jours_vfc"):
        lignes.append(
            f"• ❤️ VFC moyenne : {stats['vfc_moyenne_ms']} ms "
            f"({stats['vfc_min_ms']}–{stats['vfc_max_ms']}) sur {stats['nb_jours_vfc']} j, "
            f"FC repos {stats['fc_repos_moyenne']} bpm, sommeil {stats['sommeil_moyen_h']} h"
        )
    if stats.get("poids_moyen_kg"):
        lignes.append(f"• ⚖️ Poids moyen : {stats['poids_moyen_kg']} kg")
    if stats.get("ctl_debut") is not None and stats.get("ctl_fin") is not None:
        delta = round(stats["ctl_fin"] - stats["ctl_debut"], 1)
        signe = "+" if delta >= 0 else ""
        lignes.append(
            f"• 💪 Forme : CTL {stats['ctl_debut']} → {stats['ctl_fin']} "
            f"({signe}{delta}), TSB moyen {stats['tsb_moyen']}"
        )
    if precedent and precedent.get("km"):
        evol = round((stats["km"] - precedent["km"]) / precedent["km"] * 100)
        signe = "+" if evol >= 0 else ""
        lignes.append(
            f"• 🆚 vs {_libelle_mois(mois_prec)} : {signe}{evol} % de km "
            f"({stats['km']} vs {precedent['km']})"
        )
    if records["premiere_fois"]:
        lignes.append("• 🏆 Records initialisés : " + ", ".join(
            f"{cle.replace('_', ' ')} ({rec['valeur']} {rec['unite']})"
            for cle, rec in sorted(records["records"].items())))
    elif records["nouveaux"]:
        lignes.append("• 🏆 Nouveaux records : " + ", ".join(
            f"{cle.replace('_', ' ')} ({records['records'][cle]['valeur']} "
            f"{records['records'][cle]['unite']})"
            for cle in records["nouveaux"]))
    if analyse:
        lignes += ["", f"🧠 Analyse IA : {analyse}"]
    else:
        lignes += ["", f"🧠 Analyse IA : indisponible — raison : {raison_erreur}"]
    bulletin = "\n".join(lignes)
    (MONTHLY_DIR / "bulletin.txt").write_text(bulletin)

    # Graphiques + email (best effort : n'interrompt jamais le bulletin)
    dossier_graphes = pathlib.Path(tempfile.mkdtemp(prefix="mensuel_"))
    try:
        graphiques, erreur_graphiques = _graphiques_mensuels(parsed, jours, dossier_graphes)
        if erreur_graphiques:
            print(f"⚠️ Graphiques partiels ou absents : {erreur_graphiques}")
            result["graphiques_erreur"] = erreur_graphiques
            chemin_resultat.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        graphiques = []
        result["graphiques_erreur"] = f"{type(e).__name__} : {e}"
        print(f"⚠️ Graphiques non générés : {result['graphiques_erreur']}")
        chemin_resultat.write_text(json.dumps(result, ensure_ascii=False, indent=2))

    sujet = f"📊 Bulletin mensuel — {_libelle_mois(mois)} : {stats['km']} km"
    erreur_email = _envoi_email(sujet, bulletin, graphiques)
    if erreur_email:
        print(f"Email non envoyé — raison : {erreur_email}")
        result["email_erreur"] = erreur_email
        chemin_resultat.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Bulletin envoyé par email ✉️")

    shutil.rmtree(dossier_graphes, ignore_errors=True)
    print(bulletin)


if __name__ == "__main__":
    main()
