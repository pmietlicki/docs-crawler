# 📚 docs-crawler

*Scripts Python pour exporter le contenu d’un site Confluence interne (pages + PDF) et crawler un site Docktail afin de rapatrier tous les documents (HTML, PDF, Office…).*

---

## Sommaire
1. [Fonctionnalités](#fonctionnalités)
2. [Prérequis](#prérequis)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Exécution rapide](#exécution-rapide)
6. [Détails des scripts](#détails-des-scripts)
7. [Trucs & Astuces](#trucs--astuces)
8. [Licence](#licence)

---

## Fonctionnalités

| Script          | Objectif principal                                                                                              |
|-----------------|------------------------------------------------------------------------------------------------------------------|
| **`confluence.py`** | • Exporte toutes les pages (_page_ & _blogpost_) d’un site Confluence en JSON + PDF <br>• Filtre optionnel par préfixe d’espace |
| **`crawler.py`**    | • Crawler récursif d’un site protégé (auth Basic) <br>• Téléchargement intelligent des documents externes <br>• Conversion et fusion PDF (optionnelles) |

---

## Prérequis

* Python 3.9+
* Linux ou WSL (libreoffice + chromium nécessaires)
* Packages : `pip install -r requirements.txt`  
  (voir contenu : **aiohttp, aiofiles, PyPDF2, pdfkit, bs4, pyppeteer, requests, pdfkit**, etc.)
* `wkhtmltopdf`, `libreoffice`, `chromium-browser` installés dans le PATH

---

## Installation

```bash
git clone https://github.com/<org>/docs-crawler.git
cd docs-crawler
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

---

## Configuration

Toutes les variables se font via **variables d’environnement** :

| Variable                     | Par défaut                    | Script          | Description                                              |
|------------------------------|-------------------------------|-----------------|----------------------------------------------------------|
| `CONFLUENCE_URL`             | `https://confluence.local`    | confluence.py   | URL racine de Confluence                                 |
| `CONFLUENCE_API_TOKEN`       | _(obligatoire)_               | confluence.py   | Jeton d’API (Bearer)                                     |
| `EXPORT_BASE_DIR`            | `/repo/confluence`            | confluence.py   | Répertoire racine d’export                               |
| `SPACE_KEY_FILTER`           | `""`                          | confluence.py   | Préfixe d’espace (ex. `PROJ` → n’exporte que PROJ*)      |
| `DOCKTAIL_USERNAME`          | _(obligatoire)_               | crawler.py      | Login Docktail (auth Basic)                              |
| `DOCKTAIL_PASSWORD`          | _(obligatoire)_               | crawler.py      | Password Docktail                                        |
| `saveDirectory`              | `./docs`                      | crawler.py      | Dossier de sortie du crawler                             |
| `baseURL`                    | `https://docktail.test.local` | crawler.py      | URL de départ du crawl                                   |
| `maxDepth`                   | `10`                          | crawler.py      | Profondeur maximale du crawl                             |

---

## Exécution rapide

### Export Confluence

```bash
CONFLUENCE_API_TOKEN=xxx \
python confluence.py
# → ./pages/*.json et ./pdfs/*.pdf remplis
```

### Crawl Docktail

```bash
DOCKTAIL_USERNAME=foo DOCKTAIL_PASSWORD=bar \
python crawler.py
# → ./docs/<arborescence>/… (HTML, documents, PDF fusionnés)
```

---

## Détails des scripts

### `confluence.py`

* Utilise l’API REST v2 pour lister tous les contenus.
* Télécharge le PDF via `/spaces/flyingpdf/pdfpageexport.action?pageId=…`.
* N’écrit un fichier (JSON ou PDF) **que** si :
  * inexistant ;
  * ou horodatage différent (évite les re-downloads inutiles).
* Slugification ASCII sûre pour les noms de fichiers.

### `crawler.py`

* Lance un **Chromium headless** (pyppeteer) avec interception des requêtes.
* Gestion complète des ressources : cache 1 h, suivi des redirects, exclusion des polices.
* Remplacement des <img> par data-URI base64 pour un HTML autonome.
* Conversion facultative Office / HTML → PDF (+ fusion progressive par dossier).
* Résilient (retries, timeouts, défilement auto, gestion routerLink Angular).

---

## Trucs & Astuces

* **Vitesse** : lancez plusieurs exports en parallèle en changeant `SPACE_KEY_FILTER`.
* **Dépannage login Docktail** : vérifier que la 2FA n’est pas activée sur le compte.
* **Limites Confluence** : l’API `/rest/api/space` renvoie max 10 000 espaces.  
  Utiliser un filtre si votre instance est très volumineuse.
* **Docker** : un `Dockerfile` minimal est fourni, permet un run isolé sans dépendances système.

---

## Licence

MIT © 2025

---
