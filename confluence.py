#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import unicodedata
import re
import logging
import shutil
from typing import List, Dict
from datetime import datetime
from urllib.parse import urljoin
from time import sleep
from pathlib import Path
import requests

# ==== CONFIGURATION ====
CONFLUENCE_URL   = os.getenv("CONFLUENCE_URL",   "https://confluence.local").rstrip("/")
BEARER_TOKEN     = os.getenv("CONFLUENCE_API_TOKEN")
if not BEARER_TOKEN:
    raise RuntimeError("Il manque l'env var CONFLUENCE_API_TOKEN")

BASE_DIR         = Path(os.getenv("EXPORT_BASE_DIR",   "/repo/confluence"))
EXPORT_DIR_PAGES = Path(os.path.join(BASE_DIR, "pages"))
EXPORT_DIR_PDFS  = Path(os.path.join(BASE_DIR, "pdfs"))
SPACE_KEY_FILTER = os.getenv("SPACE_KEY_FILTER",  "")  # ""=tous, ou préfixe d'espace

# ==== LOGGER ====
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ==== SLUGIFY UTILITY ====  
def _slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii","ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or s

os.makedirs(EXPORT_DIR_PAGES, exist_ok=True)
os.makedirs(EXPORT_DIR_PDFS,  exist_ok=True)

# ==== REST SESSION (Bearer) ====  
sess = requests.Session()
sess.headers.update({
    "Authorization": f"Bearer {BEARER_TOKEN}",
    "Content-Type":  "application/json"
})

# ==== EXPORT PAGES TO JSON via REST ====  
TYPES = ["page", "blogpost"]

# ─── EXPORT PAGES + PDF (Mono-boucle) ────────────────────────────────────
def export_pages_and_pdfs() -> None:
    base_api = f"{CONFLUENCE_URL}/"
    spaces = sess.get(f"{CONFLUENCE_URL}/rest/api/space",
                      params={"limit": 10000, "status": "current"},
                      timeout=60).json()["results"]

    total_json, total_pdf = 0, 0

    for sp in spaces:
        key = sp["key"]
        if SPACE_KEY_FILTER and not key.startswith(SPACE_KEY_FILTER):
            continue

        cql   = f'space = "{key}" AND type IN ({",".join(TYPES)})'
        url   = f"{CONFLUENCE_URL}/rest/api/content/search"
        params = {"cql": cql, "limit": 500, "expand": "body.storage,version"}

        while url:
            data = sess.get(url, params=params if url.endswith("/search") else None, timeout=60).json()

            for p in data["results"]:
                pid, title, ctype = p["id"], p["title"], p["type"]
                dt   = datetime.fromisoformat(p["version"]["when"].replace("Z","+00:00"))
                ts   = dt.timestamp()

                slug = _slugify(f"{key}_{title}")
                pdf_fn = f"{dt:%Y%m%d}_{slug}_{pid}.pdf"
                pdf_dst = EXPORT_DIR_PDFS / pdf_fn

                # --- téléchargement éventuel du PDF (uniquement pour pages)
                if ctype == "page":
                    need_dl = not (pdf_dst.exists() and abs(pdf_dst.stat().st_mtime - ts) < 1)
                    if need_dl:
                        pdf_ok = False
                        try:
                            g = sess.get(f"{CONFLUENCE_URL}/spaces/flyingpdf/pdfpageexport.action",
                                         params={"pageId": pid},
                                         headers={"X-Atlassian-Token":"no-check"},
                                         allow_redirects=False, timeout=(10,30))
                            if g.status_code == 302 and "Location" in g.headers:
                                pdf_url = g.headers["Location"]
                                if not pdf_url.startswith("http"):
                                    pdf_url = f"{CONFLUENCE_URL}{pdf_url}"

                                for k in range(3):          # 3 tentatives maxi
                                    try:
                                        resp = sess.get(pdf_url, stream=True, timeout=(10,180))
                                        resp.raise_for_status()
                                        with pdf_dst.open("wb") as f:
                                            shutil.copyfileobj(resp.raw, f)
                                        os.utime(pdf_dst, (ts, ts))
                                        logging.info("PDF %s téléchargé → %s", pid, pdf_fn)
                                        total_pdf += 1
                                        pdf_ok = True
                                        break
                                    except (requests.ReadTimeout, requests.ConnectionError) as e:
                                        if k == 2:
                                            raise
                                        sleep(5*(k+1))
                            else:
                                logging.warning("Pas de PDF pour page %s (HTTP %s)", pid, g.status_code)
                        except Exception as e:
                            logging.error("❌ PDF page %s ignoré : %s", pid, e)

                # --- écriture JSON (toujours)
                meta = {
                    "id": pid, "space": key, "title": title,
                    "url": f"{CONFLUENCE_URL}/pages/viewpage.action?pageId={pid}",
                    "last_modified": p["version"]["when"],
                    "html": p["body"]["storage"]["value"],
                    "content_type": ctype,
                    "pdf_filename": pdf_fn if (ctype=="page" and pdf_dst.exists()) else None,
                }
                slug  = _slugify(f"{key}_{title}")
                json_path = EXPORT_DIR_PAGES / f"{slug}_{pid}.json"
                json_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                total_json += 1

            nxt = data["_links"].get("next")
            url, params = (urljoin(base_api, nxt), None) if nxt else (None, None)

    logging.info("✅ %d fichiers JSON écrits – %d PDF mis à jour", total_json, total_pdf)


if __name__ == "__main__":
    try:
        export_pages_and_pdfs()
        logging.info("Export Confluence terminé.")
    except Exception as e:
        logging.error(f"Erreur lors de l'export : {e}", exc_info=True)
