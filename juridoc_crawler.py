#!/usr/bin/env python3
"""
Juridoc Deep Crawler
====================

Collecte exhaustive de https://juridoc.gouv.nc (plateforme Lotus Domino).
Le crawler interroge directement les vues Domino, agents d'export et liens
OpenElement pour récupérer l'ensemble des documents (textes, codes,
jurisprudence, JONC, etc.) sans dépendre de l'UI frame/js.

Le script produit une liste normalisée de documents et peut déclencher le
téléchargement via `DocumentDownloader`.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from document_downloader import DocumentDownloader


# ---------------------------------------------------------------------------
# Modèles de données
# ---------------------------------------------------------------------------

SUPPORTED_DOC_EXT = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".txt",
    ".rtf",
    ".htm",
    ".html",
}


@dataclass(slots=True)
class CrawlConfig:
    base_url: str = "https://juridoc.gouv.nc"
    output_dir: str = "output/juridoc"
    delay: float = 1.0
    max_documents: Optional[int] = None
    timeout: int = 30
    retries: int = 3
    save_progress: bool = True


@dataclass(slots=True)
class DocumentInfo:
    url: str
    title: str
    category: str
    section: Optional[str] = None
    source: Optional[str] = None
    doc_type: Optional[str] = None
    discovered_at: str = field(
        default_factory=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    )

    def to_dict(self) -> Dict[str, str]:
        payload = {
            "url": self.url,
            "title": self.title,
            "category": self.category,
            "discovered_at": self.discovered_at,
        }
        if self.section:
            payload["section"] = self.section
        if self.source:
            payload["source"] = self.source
        if self.doc_type:
            payload["doc_type"] = self.doc_type
        return payload


# ---------------------------------------------------------------------------
# Crawler principal
# ---------------------------------------------------------------------------

class JuridocCrawler:
    def __init__(self, config: CrawlConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "JuridocCrawler/1.0 (+https://pascal-mietlicki.fr/projects/juridoc)"
                ),
                "Accept": "*/*",
            }
        )
        self.start_time = time.time()
        self.documents: List[DocumentInfo] = []
        self.doc_urls: Set[str] = set()
        self.pages_seen: int = 0
        self.stats = {
            "documents_found": 0,
            "errors": 0,
            "retries": 0,
            "pages_crawled": 0,
            "sections_explored": set(),
            "document_types": {},
            "unique_domains": set(),
        }

    # ------------------------------------------------------------------ Utils

    def log(self, message: str) -> None:
        timestamp = datetime.utcnow().strftime("%H:%M:%S")
        print(f"[{timestamp}] {message}")

    def _sleep(self) -> None:
        if self.config.delay > 0:
            time.sleep(self.config.delay)

    def _full_url(self, href: str) -> str:
        return urljoin(self.config.base_url, href)

    def _request(
        self, url: str, *, stream: bool = False, expected_json: bool = False
    ) -> requests.Response:
        attempt = 0
        while True:
            try:
                resp = self.session.get(
                    url,
                    timeout=self.config.timeout,
                    stream=stream,
                )
                resp.raise_for_status()
                return resp
            except Exception as exc:
                attempt += 1
                self.stats["errors"] += 1
                if attempt >= self.config.retries:
                    raise
                self.stats["retries"] += 1
                self.log(
                    f"⚠️  Échec {attempt}/{self.config.retries} sur {url}: {exc}. Retry…"
                )
                time.sleep(min(5 * attempt, 30))

    def _get_html(self, path: str) -> BeautifulSoup:
        url = self._full_url(path)
        resp = self._request(url)
        resp.encoding = resp.apparent_encoding or "utf-8"
        self.stats["pages_crawled"] += 1
        return BeautifulSoup(resp.text, "html.parser")

    def _get_json(self, url: str) -> dict:
        if not url.lower().startswith("http"):
            url = self._full_url(url)
        resp = self._request(url)
        self.stats["pages_crawled"] += 1
        if resp.text.startswith("jsonLoad("):
            payload = resp.text.strip()
            if payload.endswith(");"):
                payload = payload[len("jsonLoad(") : -2]
            return json.loads(payload)
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.json()

    def _detect_doc_type(self, url: str) -> str:
        path = urlparse(url).path
        ext = os.path.splitext(path)[1].lower()
        if ext in SUPPORTED_DOC_EXT:
            if ext in {".htm", ".html"}:
                return "html"
            return ext.lstrip(".")
        return "binary"

    def _register_section(self, section: Optional[str]) -> None:
        if section:
            self.stats["sections_explored"].add(section)

    def _register_doc_type(self, doc_type: str) -> None:
        if doc_type not in self.stats["document_types"]:
            self.stats["document_types"][doc_type] = 0
        self.stats["document_types"][doc_type] += 1

    def _register_domain(self, url: str) -> None:
        domain = urlparse(url).netloc
        if domain:
            self.stats["unique_domains"].add(domain)

    def _reached_limit(self) -> bool:
        return (
            self.config.max_documents is not None
            and self.stats["documents_found"] >= self.config.max_documents
        )

    def _add_document(
        self,
        url: str,
        title: str,
        category: str,
        *,
        section: Optional[str] = None,
        source: Optional[str] = None,
    ) -> None:
        if not url or url.startswith("javascript:"):
            return
        full_url = self._full_url(url)
        if full_url in self.doc_urls:
            return
        doc_type = self._detect_doc_type(full_url)
        self.documents.append(
            DocumentInfo(
                url=full_url,
                title=title or os.path.basename(urlparse(full_url).path) or category,
                category=category,
                section=section,
                source=source,
                doc_type=doc_type,
            )
        )
        self.doc_urls.add(full_url)
        self.stats["documents_found"] += 1
        self._register_doc_type(doc_type)
        self._register_section(section)
        self._register_domain(full_url)
        if self.stats["documents_found"] % 100 == 0:
            self.log(
                f"   → {self.stats['documents_found']} documents collectés "
                f"(dernier: {category})"
            )

    def _extract_links_from_html(
        self, html_fragment: str
    ) -> Iterable[Tuple[str, str]]:
        if not html_fragment:
            return []
        soup = BeautifulSoup(html_fragment, "html.parser")
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            text = anchor.get_text(" ", strip=True)
            yield href, text

    # -------------------------------------------------------------- Collectors

    def _iter_view_entries(
        self, view_url: str, *, page_size: int = 200
    ) -> Iterable[str]:
        start = 1
        total_processed = 0
        while True:
            url = f"{self.config.base_url}{view_url}&start={start}&count={page_size}"
            data = self._get_json(url)
            entries = data.get("viewentry", [])
            if not entries:
                break
            if isinstance(entries, dict):
                entries = [entries]

            for entry in entries:
                entrydata = entry.get("entrydata", [])
                for field in entrydata:
                    text = None
                    if "text" in field:
                        value = field["text"]
                        if isinstance(value, dict):
                            text = value.get("0") or next(
                                iter(value.values()), None
                            )
                        elif isinstance(value, list):
                            text = " ".join(value)
                        elif isinstance(value, str):
                            text = value
                    elif "textlist" in field:
                        value = field["textlist"]
                        texts = value.get("text", [])
                        if isinstance(texts, list):
                            text = " ".join(texts)
                        elif isinstance(texts, str):
                            text = texts
                    if text:
                        yield text

            total_processed += len(entries)
            start += len(entries)
            if len(entries) < page_size:
                break
            if (
                self.config.max_documents
                and self.stats["documents_found"] >= self.config.max_documents
            ):
                break

    def _collect_textes(self) -> None:
        self.log("📚 Collecte des textes consolidés")
        self.log("   (vue Domino `(web-All)` - ~3 000 entrées)")
        view_path = "/JuriDoc/JdTextes.nsf/(web-All)?ReadViewEntries&outputformat=json"
        for fragment in self._iter_view_entries(view_path):
            for href, label in self._extract_links_from_html(fragment):
                if "OpenElement" in href or "OpenDocument" in href:
                    self._add_document(
                        href,
                        label,
                        category="Textes consolidés",
                        source=view_path,
                    )
                    if self._reached_limit():
                        return
            self._sleep()

    def _collect_jurisprudence(self) -> None:
        self.log("⚖️  Collecte de la jurisprudence")
        self.log("   (vue Domino `(web-All)` - ~8 000 entrées)")
        view_path = "/JuriDoc/JdJuris.nsf/(web-All)?ReadViewEntries&outputformat=json"
        for fragment in self._iter_view_entries(view_path):
            for href, label in self._extract_links_from_html(fragment):
                if "OpenElement" in href or "OpenDocument" in href:
                    self._add_document(
                        href,
                        label,
                        category="Jurisprudence",
                        source=view_path,
                    )
                    if self._reached_limit():
                        return
            self._sleep()

    def _collect_codes(self) -> None:
        self.log("📘 Collecte des codes et recueils")
        soup = self._get_html("/JuriDoc/JdCodes.nsf/ListeCodes?OpenPage")
        codes: Set[str] = set()
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            text = anchor.get_text(" ", strip=True)
            if "(globalFTP)" in href or "(mesX-FTP)" in href:
                self._add_document(
                    href,
                    text or "Téléchargement code (PDF)",
                    category="Codes - PDF",
                    source="ListeCodes?OpenPage",
                )
            else:
                parsed = urlparse(href)
                params = parse_qs(parsed.query)
                if "code" in params:
                    codes.add(params["code"][0])

        for code in sorted(codes):
            export_url = f"/JuriDoc/JdCodes.nsf/export?openagent&code=jd{code}"
            try:
                data = self._get_json(export_url)
            except Exception as exc:
                self.log(f"❌ Impossible de charger l'export du code {code}: {exc}")
                continue

            items = data.get("items", [])
            if len(items) < 2:
                continue
            base = items[0].get("base", "")
            plan = items[1].get("plan", code.upper())
            for item in items[2:]:
                identifier = item.get("i")
                key = item.get("k")
                title = item.get("t", "")
                if not identifier or not key or not title:
                    continue
                if identifier.startswith("*"):
                    doc_url = f"{base}{key}?OpenDocument"
                    self._add_document(
                        doc_url,
                        f"{plan} - {title}",
                        category="Codes",
                        section=plan,
                        source=f"export?code=jd{code}",
                    )
                    if self._reached_limit():
                        return
            self._sleep()
            if self._reached_limit():
                return

    def _collect_jonc(self) -> None:
        self.log("🗞️  Collecte du Journal Officiel (JONC)")
        soup = self._get_html("/JuriDoc/JdJonc.nsf/ListeJonc?OpenPage")
        year_options = [
            opt.get("value")
            for opt in soup.select("select[name='numAnneeJonc'] option")
            if opt.get("value")
        ]
        for year in year_options:
            self.log(f"   → Année {year}")
            path = f"/JuriDoc/JdJonc.nsf/(wJonc)?openagent&debJonc=01/01/{year}&finJonc=31/12/{year}"
            html = self._get_html(path)
            for href, label in self._extract_links_from_html(str(html)):
                if "$File" in href and "OpenElement" in href:
                    self._add_document(
                        href,
                        label or f"JONC {year}",
                        category="Journal Officiel",
                        section=year,
                        source=path,
                    )
                    if self._reached_limit():
                        return
                elif "JdJoTa.nsf/wSommaire" in href:
                    self._add_document(
                        href,
                        label or f"Sommaire JONC {year}",
                        category="Journal Officiel - Sommaires",
                        section=year,
                        source=path,
                    )
                    if self._reached_limit():
                        return
            self._sleep()
            if self._reached_limit():
                return

    def run(self) -> Dict[str, object]:
        self._collect_textes()
        if not self._reached_limit():
            self._collect_jurisprudence()
        if not self._reached_limit():
            self._collect_codes()
        if not self._reached_limit():
            self._collect_jonc()

        duration = time.time() - self.start_time
        stats = {
            "start_time": self.start_time,
            "pages_crawled": self.stats["pages_crawled"],
            "documents_found": self.stats["documents_found"],
            "errors": self.stats["errors"],
            "retries": self.stats["retries"],
            "max_depth_reached": 0,
            "sections_explored": sorted(self.stats["sections_explored"]),
            "unique_domains": sorted(self.stats["unique_domains"]),
            "document_types": self.stats["document_types"],
            "crawl_duration": duration,
            "documents_per_second": (
                self.stats["documents_found"] / duration if duration > 0 else 0
            ),
            "total_documents": len(self.documents),
            "unique_urls_visited": len(self.doc_urls),
        }
        return {"success": True, "documents": [d.to_dict() for d in self.documents], "statistics": stats}


# ---------------------------------------------------------------------------
# Entrée en ligne de commande
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawler exhaustif de juridoc.gouv.nc (Lotus Domino)."
    )
    parser.add_argument(
        "--base-url",
        default="https://juridoc.gouv.nc",
        help="URL de base du portail Juridoc",
    )
    parser.add_argument(
        "--output-dir",
        default="output/juridoc",
        help="Répertoire où stocker les résultats",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Délai (s) entre les requêtes pour éviter les surcharges",
    )
    parser.add_argument(
        "--max-documents",
        type=int,
        default=None,
        help="Nombre maximum de documents à collecter (debug)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Timeout HTTP en secondes",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Nombre de tentatives en cas d'échec réseau",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Ne pas sauvegarder d'état intermédiaire (option sans effet actuellement)",
    )
    # Options de téléchargement
    parser.add_argument(
        "--download",
        action="store_true",
        help="Télécharger les documents découverts",
    )
    parser.add_argument(
        "--download-dir",
        default=None,
        help="Répertoire de téléchargement (défaut: <output-dir>/downloads)",
    )
    parser.add_argument(
        "--download-workers",
        type=int,
        default=4,
        help="Téléchargements parallèles",
    )
    parser.add_argument(
        "--download-delay",
        type=float,
        default=0.5,
        help="Délai entre téléchargements",
    )
    parser.add_argument(
        "--max-downloads",
        type=int,
        default=None,
        help="Limiter le nombre de documents téléchargeables (debug)",
    )
    parser.add_argument(
        "--max-download-size-mb",
        type=int,
        default=100,
        help="Taille maximale d'un fichier lors du téléchargement",
    )
    parser.add_argument(
        "--disable-md5",
        action="store_true",
        help="Désactiver la vérification MD5 des téléchargements",
    )
    parser.add_argument(
        "--organize-by-type",
        dest="organize_by_type",
        action="store_true",
        help="Organiser les téléchargements par type (défaut)",
    )
    parser.add_argument(
        "--no-organize-by-type",
        dest="organize_by_type",
        action="store_false",
        help="Ne pas créer de sous-dossiers par type",
    )
    parser.set_defaults(organize_by_type=True)
    parser.add_argument(
        "--organize-by-category",
        dest="organize_by_category",
        action="store_true",
        help="Organiser les téléchargements par catégorie/section (défaut)",
    )
    parser.add_argument(
        "--no-organize-by-category",
        dest="organize_by_category",
        action="store_false",
        help="Ne pas créer de sous-dossiers par catégorie/section",
    )
    parser.set_defaults(organize_by_category=True)
    parser.add_argument(
        "--organize-by-date",
        action="store_true",
        help="Organiser les téléchargements par date (AAAA/MM)",
    )
    return parser.parse_args(argv)


def save_results(output_dir: str, result: Dict[str, object]) -> None:
    os.makedirs(output_dir, exist_ok=True)
    results_path = os.path.join(output_dir, "deep_crawl_results.json")
    with open(results_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)

    summary = {
        "crawler_version": "JuridocCrawler 2.0",
        "crawl_timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "statistics": result["statistics"],
        "total_documents": len(result["documents"]),
    }
    summary_path = os.path.join(output_dir, "deep_crawl_summary.json")
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)

    urls_path = os.path.join(output_dir, "document_urls.txt")
    with open(urls_path, "w", encoding="utf-8") as fh:
        for doc in result["documents"]:
            fh.write(f"{doc['url']}\n")


def maybe_download(args: argparse.Namespace, result: Dict[str, object]) -> Optional[dict]:
    if not args.download or not result["documents"]:
        return None
    download_dir = args.download_dir or os.path.join(args.output_dir, "downloads")
    download_config = {
        "download_dir": download_dir,
        "max_workers": args.download_workers,
        "delay": args.download_delay,
        "timeout": args.timeout,
        "max_retries": args.max_retries,
        "max_file_size_mb": args.max_download_size_mb,
        "resume": True,
        "organize_by_type": args.organize_by_type,
        "organize_by_category": args.organize_by_category,
        "organize_by_date": args.organize_by_date,
        "enable_md5_verification": not args.disable_md5,
    }
    downloader = DocumentDownloader(download_config)
    stats = downloader.download_documents(
        result["documents"], max_documents=args.max_downloads
    )
    return stats


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)

    config = CrawlConfig(
        base_url=args.base_url,
        output_dir=args.output_dir,
        delay=args.delay,
        max_documents=args.max_documents,
        timeout=args.timeout,
        retries=args.max_retries,
        save_progress=not args.no_progress,
    )

    crawler = JuridocCrawler(config)
    crawler.log("🚀 Lancement du crawler Juridoc")
    result = crawler.run()

    save_results(args.output_dir, result)

    download_stats = maybe_download(args, result)

    duration = result["statistics"]["crawl_duration"]
    crawler.log("🎉 Collecte terminée avec succès")
    crawler.log(
        f"   Documents: {result['statistics']['documents_found']} | "
        f"Pages interrogées: {result['statistics']['pages_crawled']} | "
        f"Durée: {duration:.1f}s"
    )
    if download_stats:
        crawler.log(
            f"   Téléchargés: {download_stats['downloaded']} (ignorés: {download_stats['skipped']}, erreurs: {download_stats['errors']})"
        )
        crawler.log(f"   Volume total: {download_stats['total_size_mb']:.1f} MB")


if __name__ == "__main__":
    main()
