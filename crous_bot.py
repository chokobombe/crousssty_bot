#!/usr/bin/env python3
"""
Bot de veille "logement Crous disponible à Toulouse" avec notification Telegram.

Fonctionnement :
- Interroge la page de recherche publique de trouverunlogement.lescrous.fr
  pour les campagnes configurées (par défaut : année en cours + année suivante).
- Le filtre géographique de ce site est appliqué en JavaScript côté navigateur,
  donc une requête HTTP simple ne peut pas filtrer par ville de façon fiable.
  Le bot récupère donc toutes les annonces, puis garde uniquement celles dont
  l'adresse contient le nom de la ville recherchée (par défaut "Toulouse").
- Compare les annonces trouvées à celles déjà vues (stockées dans un fichier
  JSON local) et envoie un message Telegram uniquement pour les NOUVELLES
  annonces.

Configuration : voir le fichier .env (copier .env.example -> .env et remplir).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE_URL = "https://trouverunlogement.lescrous.fr"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Ville recherchée (comparaison insensible à la casse sur l'adresse affichée)
CITY_FILTER = os.environ.get("CROUS_CITY", "Toulouse")

# Identifiants des campagnes ("tools/<id>/search") à surveiller.
# 42 = année en cours (2025-2026), 47 = année prochaine (2026-2027)
# au moment de l'écriture de ce script. Ces identifiants peuvent changer
# chaque année : va sur https://trouverunlogement.lescrous.fr, lance une
# recherche, et regarde le nombre dans l'URL /tools/<id>/search.
TOOL_IDS = [
    int(x) for x in os.environ.get("CROUS_TOOL_IDS", "47").split(",") if x.strip()
]

CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "180"))  # 3 min
MAX_PAGES_PER_TOOL = int(os.environ.get("MAX_PAGES_PER_TOOL", "15"))
REQUEST_TIMEOUT = 20
STATE_FILE = Path(os.environ.get("STATE_FILE", "seen_accommodations.json"))

# Cookie de session Crous (optionnel). Si renseigné, le bot verra les mêmes
# annonces que toi une fois connecté (contingent boursier inclus). A
# récupérer manuellement depuis le navigateur (voir README) et à renouveler
# de temps en temps car les sessions expirent.
CROUS_SESSION_COOKIE = os.environ.get("CROUS_SESSION_COOKIE", "").strip()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("crous_bot")

ACCOMMODATION_RE = re.compile(r"/tools/(\d+)/accommodations/(\d+)")
PRICE_RE = re.compile(r"(\d[\d\s]*,?\d*)\s*€")
SURFACE_RE = re.compile(r"(\d[\d,\.]*)\s*m²")


@dataclass
class Listing:
    tool_id: str
    acc_id: str
    title: str
    address: str
    price: str
    surface: str
    url: str

    @property
    def key(self) -> str:
        return f"{self.tool_id}:{self.acc_id}"


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #

_session_warning_shown = False


def fetch_page(tool_id: int, page: int) -> str:
    global _session_warning_shown
    url = f"{BASE_URL}/tools/{tool_id}/search"
    params = {"page": page} if page > 1 else {}
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "fr-FR,fr;q=0.9"}
    if CROUS_SESSION_COOKIE:
        headers["Cookie"] = CROUS_SESSION_COOKIE

    resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    html = resp.text

    if CROUS_SESSION_COOKIE and not _session_warning_shown:
        # Détection best-effort d'une session expirée : si le lien
        # "Identification" (connexion) est toujours présent tel quel, la
        # session n'a probablement pas été prise en compte.
        if "Identification" in html and "Déconnexion" not in html:
            log.warning(
                "⚠️ Le cookie de session Crous semble expiré ou invalide "
                "(le site ne te reconnaît pas comme connecté). Les annonces "
                "réservées boursiers ne seront pas visibles tant que tu "
                "n'auras pas fourni un nouveau cookie."
            )
            _session_warning_shown = True

    return html


def parse_listings(html: str, tool_id: int) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    listings: list[Listing] = []
    seen_ids_on_page: set[str] = set()

    for a in soup.find_all("a", href=True):
        match = ACCOMMODATION_RE.search(a["href"])
        if not match:
            continue
        acc_id = match.group(2)
        if acc_id in seen_ids_on_page:
            continue
        seen_ids_on_page.add(acc_id)

        title = a.get_text(strip=True)
        if not title:
            continue  # ce lien est probablement l'image, pas le titre

        # On remonte dans l'arbre HTML pour retrouver prix / adresse /
        # surface, mais JAMAIS au point de mélanger le texte d'une autre
        # annonce voisine : on s'arrête dès que le conteneur contiendrait
        # plus d'une seule annonce (sécurité anti-contamination).
        container = a
        text = a.get_text(" ", strip=True)
        for _ in range(8):
            if container.parent is None:
                break
            candidate = container.parent
            candidate_text = candidate.get_text(" ", strip=True)
            ids_in_candidate = {
                m.group(2)
                for link in candidate.find_all("a", href=True)
                if (m := ACCOMMODATION_RE.search(link["href"]))
            }
            if len(ids_in_candidate) > 1:
                # On a dépassé la carte de cette annonce : on garde le
                # dernier conteneur valide (une seule annonce dedans).
                break
            container = candidate
            text = candidate_text
            if "€" in text and "m²" in text:
                break

        price_match = PRICE_RE.search(text)
        surface_match = SURFACE_RE.search(text)

        # L'adresse est en général juste après le titre, avant la surface.
        address = text
        idx_title = text.find(title)
        if idx_title != -1:
            address = text[idx_title + len(title):]
        if surface_match:
            address = address[: address.find(surface_match.group(0))]
        address = address.strip(" -–,")

        href = a["href"]
        full_url = href if href.startswith("http") else BASE_URL + href

        listings.append(
            Listing(
                tool_id=str(tool_id),
                acc_id=acc_id,
                title=title,
                address=address,
                price=price_match.group(0) if price_match else "?",
                surface=surface_match.group(0) if surface_match else "?",
                url=full_url,
            )
        )

    return listings


def fetch_all_listings(tool_id: int) -> list[Listing]:
    all_listings: dict[str, Listing] = {}
    for page in range(1, MAX_PAGES_PER_TOOL + 1):
        try:
            html = fetch_page(tool_id, page)
        except requests.RequestException as exc:
            log.warning("Erreur réseau tool=%s page=%s: %s", tool_id, page, exc)
            break

        page_listings = parse_listings(html, tool_id)
        new_on_page = [l for l in page_listings if l.key not in all_listings]

        if not page_listings:
            break

        for l in page_listings:
            all_listings[l.key] = l

        if not new_on_page and page > 1:
            # page probablement identique à la précédente -> fin de pagination
            break

        # Pas de lien "page suivante" -> on arrête
        if f"?page={page + 1}" not in html and f"page={page + 1}" not in html:
            break

    return list(all_listings.values())


def filter_by_city(listings: Iterable[Listing], city: str) -> list[Listing]:
    city_lower = city.lower()
    return [l for l in listings if city_lower in l.address.lower() or city_lower in l.title.lower()]


# --------------------------------------------------------------------------- #
# Etat local (anti-doublons)
# --------------------------------------------------------------------------- #

def load_seen() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return set()


def save_seen(seen: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen)), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def send_telegram_message(text: str, _retry_count: int = 0) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant, message non envoyé.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 429 and _retry_count < 3:
            # Telegram limite le nombre de messages par seconde. On respecte
            # le délai qu'il indique lui-même avant de réessayer.
            retry_after = 5
            try:
                retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
            except (ValueError, KeyError):
                pass
            log.warning(
                "Telegram rate limit atteint, nouvelle tentative dans %ss...", retry_after
            )
            time.sleep(retry_after + 1)
            send_telegram_message(text, _retry_count=_retry_count + 1)
            return
        if not resp.ok:
            log.error("Echec envoi Telegram: %s %s", resp.status_code, resp.text)
    except requests.RequestException as exc:
        log.error("Erreur réseau Telegram: %s", exc)


def format_listing_message(l: Listing) -> str:
    return (
        f"🏠 <b>Nouveau logement Crous à {CITY_FILTER}</b>\n\n"
        f"<b>{l.title}</b>\n"
        f"📍 {l.address}\n\n"
        f"{l.url}"
    )


# --------------------------------------------------------------------------- #
# Boucle principale
# --------------------------------------------------------------------------- #

def run_once(seen: set[str], first_run: bool) -> set[str]:
    matched: list[Listing] = []
    for tool_id in TOOL_IDS:
        try:
            listings = fetch_all_listings(tool_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("Erreur lors de la récupération tool_id=%s: %s", tool_id, exc)
            continue
        city_listings = filter_by_city(listings, CITY_FILTER)
        log.info(
            "tool_id=%s : %d annonces au total, %d à %s",
            tool_id, len(listings), len(city_listings), CITY_FILTER,
        )
        matched.extend(city_listings)

    new_listings = [l for l in matched if l.key not in seen]

    if first_run:
        log.info(
            "Premier lancement : %d logement(s) déjà présent(s) marqué(s) comme "
            "vus sans notification. Les prochaines nouveautés seront notifiées.",
            len(new_listings),
        )
    else:
        for l in new_listings:
            log.info("Nouveau logement détecté : %s (%s)", l.title, l.address)
            send_telegram_message(format_listing_message(l))
            time.sleep(1.2)  # espacement préventif anti rate-limit Telegram

    seen.update(l.key for l in matched)
    return seen


def main() -> None:
    once = "--once" in sys.argv

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error(
            "Merci de configurer TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID "
            "(dans un fichier .env ou en variables d'environnement)."
        )
        sys.exit(1)

    seen = load_seen()
    first_run = not seen

    log.info(
        "Démarrage du bot. Ville=%s, campagnes=%s, intervalle=%ss, mode=%s",
        CITY_FILTER, TOOL_IDS, CHECK_INTERVAL_SECONDS,
        "connecté (cookie fourni)" if CROUS_SESSION_COOKIE else "public (sans connexion)",
    )

    # Message de test envoyé au démarrage, pour vérifier que la connexion
    # Telegram fonctionne (token + chat_id corrects). Uniquement en mode
    # boucle continue : en mode --once (ex. GitHub Actions), le script est
    # relancé à chaque vérification, donc ce message spammerait sinon.
    if not once:
        send_telegram_message(
            f"✅ Bot Crous {CITY_FILTER} démarré. Surveillance en cours "
            f"(vérification toutes les {CHECK_INTERVAL_SECONDS // 60} min)."
        )

    if once:
        seen = run_once(seen, first_run)
        save_seen(seen)
        return

    while True:
        try:
            seen = run_once(seen, first_run)
            first_run = False
            save_seen(seen)
        except Exception as exc:  # noqa: BLE001
            log.exception("Erreur inattendue dans la boucle principale: %s", exc)
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
