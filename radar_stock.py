"""
radar_stock.py — version "cloud"
==================================
Conçu pour être exécuté périodiquement par GitHub Actions (voir
.github/workflows/radar.yml), pas par une boucle infinie sur ton PC.

Chaque exécution fait UNE vérification de chaque produit, puis s'arrête.
L'état précédent (en stock / pas en stock) est lu et sauvegardé dans
state.json, qui est committé dans le repo par le workflow pour survivre
d'une exécution à l'autre.

Dépendances : requests, beautifulsoup4 (voir requirements.txt)
"""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("radar")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

STATE_FILE = Path(__file__).parent / "state.json"

# Le topic ntfy est lu depuis une variable d'environnement (renseignée via
# un secret GitHub Actions) pour ne JAMAIS apparaître en clair dans le code,
# même si le repo est public. Voir le README pour la configuration.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")


@dataclass
class Produit:
    id: str  # identifiant stable, sert de clé dans state.json
    nom: str
    url: str
    verif_stock: Callable[[BeautifulSoup], bool]


def charger_etat() -> Dict[str, bool]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def sauvegarder_etat(etat: Dict[str, bool]) -> None:
    STATE_FILE.write_text(json.dumps(etat, indent=2, ensure_ascii=False))


def verifier_produit(produit: Produit) -> Optional[bool]:
    try:
        resp = requests.get(produit.url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"[{produit.nom}] Erreur réseau : {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    try:
        return produit.verif_stock(soup)
    except Exception as e:
        logger.warning(f"[{produit.nom}] Erreur d'analyse HTML : {e}")
        return None


def notifier_ntfy(titre: str, message: str, topic: str) -> None:
    if not topic:
        logger.warning("NTFY_TOPIC non configuré, notification ignorée.")
        return
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            # CORRECTION ICI : titre.encode("utf-8") pour accepter l'émoji
            headers={"Title": titre.encode("utf-8"), "Priority": "high", "Tags": "bell"},
            timeout=10,
        )
    except requests.RequestException as e:
        logger.warning(f"Erreur envoi ntfy : {e}")


# ---------------------------------------------------------------------
# Règles de détection de stock par site — À VÉRIFIER/ADAPTER
# Inspecte chaque page (clic droit > Inspecter) pour repérer le bon
# indicateur si les mots-clés génériques ci-dessous ne suffisent pas.
# ---------------------------------------------------------------------

def stock_generique_mots_cles(soup: BeautifulSoup, mot_dispo: str, mots_rupture: list[str]) -> bool:
    texte = soup.get_text().lower()
    if any(mot.lower() in texte for mot in mots_rupture):
        return False
    return mot_dispo.lower() in texte


def stock_par_schema_org(soup: BeautifulSoup) -> Optional[bool]:
    """Beaucoup de sites e-commerce embarquent des données structurées
    schema.org (itemprop="availability") indiquant la disponibilité —
    quand c'est présent, c'est bien plus fiable qu'une recherche de
    mots-clés dans le texte. Renvoie None si rien n'est trouvé, pour
    laisser un fallback par mots-clés prendre le relais."""
    tag = soup.find(attrs={"itemprop": "availability"})
    if tag:
        valeur = (tag.get("href") or tag.get("content") or tag.get_text() or "").lower()
        if "instock" in valeur or "limitedavailability" in valeur or "preorder" in valeur:
            return True
        if "outofstock" in valeur or "soldout" in valeur or "discontinued" in valeur:
            return False
    return None


def stock_leclerc(soup: BeautifulSoup) -> bool:
    return stock_generique_mots_cles(soup, "ajouter au panier", ["précommande épuisée", "rupture"])


def stock_cultura(soup: BeautifulSoup) -> bool:
    r = stock_par_schema_org(soup)
    if r is not None:
        return r
    return stock_generique_mots_cles(soup, "ajouter au panier", ["indisponible", "rupture de stock"])


def stock_micromania(soup: BeautifulSoup) -> bool:
    r = stock_par_schema_org(soup)
    if r is not None:
        return r
    return stock_generique_mots_cles(soup, "je précommande", ["rupture", "indisponible"])


def stock_boulanger(soup: BeautifulSoup) -> bool:
    r = stock_par_schema_org(soup)
    if r is not None:
        return r
    return stock_generique_mots_cles(soup, "ajouter au panier", ["indisponible", "rupture de stock"])


def stock_carrefour(soup: BeautifulSoup) -> bool:
    r = stock_par_schema_org(soup)
    if r is not None:
        return r
    return stock_generique_mots_cles(soup, "ajouter au panier", ["indisponible", "rupture"])


def stock_darty(soup: BeautifulSoup) -> bool:
    r = stock_par_schema_org(soup)
    if r is not None:
        return r
    return stock_generique_mots_cles(soup, "ajouter au panier", ["indisponible", "rupture de stock"])


def stock_amazon(soup: BeautifulSoup) -> bool:
    """Amazon a un système anti-bot agressif : il sert parfois une page
    captcha ("Saisissez les caractères...") au lieu de la vraie page produit.
    On lève une exception dans ce cas plutôt que de renvoyer False, pour
    NE PAS confondre "bloqué par Amazon" avec "vraiment en rupture" — ça
    évite de fausser l'état sauvegardé dans state.json."""
    texte = soup.get_text().lower()
    if "saisissez les caractères" in texte or "vérification de sécurité" in texte:
        raise RuntimeError("page anti-bot Amazon (captcha) — vérification ignorée ce cycle")

    dispo = soup.find(id="availability")
    if dispo and any(mot in dispo.get_text().lower() for mot in ["indisponible", "actuellement indisponible"]):
        return False

    bouton = soup.find(id="add-to-cart-button") or soup.find(id="buy-now-button")
    return bouton is not None


PRODUITS: List[Produit] = [
    Produit(
        "leclerc_switch40",
        "Switch 2 — 40 ans Zelda — Leclerc",
        "https://www.e.leclerc/fp/console-nintendo-switch-2-edition-40e-anniversaire-de-the-legend-of-zelda-nintendo-switch-2-0045496337292",
        stock_leclerc,
    ),
    Produit(
        "cultura_switch40",
        "Switch 2 — 40 ans Zelda — Cultura",
        "https://www.cultura.com/p-console-nintendo-switch-2-edition-limitee-the-legend-of-zelda-ocarina-of-time-13424072.html",
        stock_cultura,
    ),
    Produit(
        "micromania_switch40",
        "Switch 2 — 40 ans Zelda — Micromania",
        "https://www.micromania.fr/p/console-nintendo-switch-2-edition-limitee-40eme-anniversaire-the-legend-of-zelda-164787.html",
        stock_micromania,
    ),
    Produit(
        "amazon_switch40",
        "Switch 2 — 40 ans Zelda — Amazon",
        # ⚠️ Remplace ce lien par l'URL de LA FICHE PRODUIT exacte (copiée
        # depuis la barre d'adresse une fois sur la page), pas une page de
        # recherche : je n'ai qu'un lien de recherche affilié, pas l'ASIN
        # direct, et une page de recherche n'a pas de bouton "Ajouter au
        # panier" unique à détecter.
        "https://www.amazon.fr/s?k=Nintendo+Switch+2+Edition+40+ans+Zelda",
        stock_amazon,
    ),
    # --- Boulanger, Carrefour, Darty --------------------------------
    # Je n'ai pas trouvé d'URL directe et fiable vers LA fiche produit
    # de ces trois enseignes (seulement des liens raccourcis/affiliés).
    # Les fonctions de détection sont prêtes ci-dessus : va sur le site,
    # trouve la fiche produit, copie l'URL exacte depuis la barre
    # d'adresse, colle-la ci-dessous et décommente le bloc correspondant.
    #
    # Produit(
    #     "boulanger_switch40",
    #     "Switch 2 — 40 ans Zelda — Boulanger",
    #     "https://www.boulanger.com/URL-A-COMPLETER",
    #     stock_boulanger,
    # ),
    # Produit(
    #     "carrefour_switch40",
    #     "Switch 2 — 40 ans Zelda — Carrefour",
    #     "https://www.carrefour.fr/URL-A-COMPLETER",
    #     stock_carrefour,
    # ),
    # Produit(
    #     "darty_switch40",
    #     "Switch 2 — 40 ans Zelda — Darty",
    #     "https://www.darty.com/URL-A-COMPLETER",
    #     stock_darty,
    # ),
    #
    # Fnac retirée : elle détecte et bloque les requêtes automatisées
    # (page anti-bot quasi systématique), inutile de s'acharner dessus.
]


def main() -> None:
    etat = charger_etat()
    etat_modifie = False

    for produit in PRODUITS:
        en_stock = verifier_produit(produit)
        if en_stock is None:
            continue

        etait_en_stock = etat.get(produit.id, False)

        if en_stock and not etait_en_stock:
            titre = "✅ Stock disponible !"
            message = f"{produit.nom} est en stock : {produit.url}"
            logger.info(message)
            notifier_ntfy(titre, message, NTFY_TOPIC)
        elif en_stock:
            logger.info(f"{produit.nom} : toujours en stock (déjà notifié).")
        else:
            logger.info(f"{produit.nom} : indisponible.")

        if en_stock != etait_en_stock:
            etat[produit.id] = en_stock
            etat_modifie = True

    if etat_modifie:
        sauvegarder_etat(etat)
        logger.info("État mis à jour (state.json).")
    else:
        logger.info("Aucun changement d'état.")


if __name__ == "__main__":
    main()
