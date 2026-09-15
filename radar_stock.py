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
    )
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
            # La correction est sur la ligne ci-dessous (ajout de .encode("utf-8")) :
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


def stock_fnac(soup: BeautifulSoup) -> bool:
    return stock_generique_mots_cles(soup, "ajouter au panier", ["indisponible", "épuisé"])


def stock_leclerc(soup: BeautifulSoup) -> bool:
    return stock_generique_mots_cles(soup, "ajouter au panier", ["précommande épuisée", "rupture"])


def stock_cultura(soup: BeautifulSoup) -> bool:
    return stock_generique_mots_cles(soup, "ajouter au panier", ["indisponible", "rupture de stock"])


PRODUITS: List[Produit] = [
   # Produit(
   #     "fnac_switch40",
    #    "Switch 2 — 40 ans Zelda — Fnac",
     #   "https://www.fnac.com/Console-Nintendo-Switch-2-Edition-Limitee-40eme-anniversaire-The-Legend-of-Zelda/a21424371/w-4",
      #  stock_fnac,
    #),
    Produit(
        "leclerc_switch40",
        "Switch 2 — 40 ans Zelda — Leclerc",
        "https://www.e.leclerc/fp/console-nintendo-switch-2-edition-40e-anniversaire-de-the-legend-of-zelda-nintendo-switch-2-0045496337292",
        stock_leclerc,
    ),
    # Ajoute Amazon / Cultura / Micromania ici une fois les sélecteurs vérifiés.
    # Attention : les pages qui chargent leur contenu en JavaScript (souvent
    # le cas sur Amazon) ne peuvent pas être lues correctement avec `requests`
    # seul — il faudrait alors passer par Selenium/Playwright (me le demander).
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
