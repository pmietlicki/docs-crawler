#!/usr/bin/env python3
"""
DOCUMENT DOWNLOADER - Téléchargeur de documents juridiques
Télécharge les documents à partir des URLs découvertes par le crawler
"""

import json
import os
import time
import argparse
import requests
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse, unquote
from pathlib import Path
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import threading
import logging
from datetime import datetime

class MD5Manager:
    """
    Gestionnaire thread-safe des empreintes MD5 pour les documents
    """
    
    def __init__(self, md5_file_path: str):
        """Initialise le gestionnaire MD5"""
        self.md5_file_path = md5_file_path
        self.md5_data = {}
        self.lock = threading.RLock()  # Verrou réentrant pour thread-safety
        self.logger = logging.getLogger(f"{__name__}.MD5Manager")
        self.load_md5_data()
    
    def load_md5_data(self):
        """Charge les données MD5 depuis le fichier"""
        with self.lock:
            try:
                if os.path.exists(self.md5_file_path):
                    with open(self.md5_file_path, 'r', encoding='utf-8') as f:
                        self.md5_data = json.load(f)
                    self.logger.info(f"Chargé {len(self.md5_data)} empreintes MD5 depuis {self.md5_file_path}")
                else:
                    self.md5_data = {}
                    self.logger.info("Nouveau fichier MD5 créé")
            except Exception as e:
                self.logger.error(f"Erreur lors du chargement des données MD5: {e}")
                self.md5_data = {}
    
    def save_md5_data(self):
        """Sauvegarde les données MD5 dans le fichier"""
        with self.lock:
            try:
                # Créer le répertoire parent si nécessaire
                os.makedirs(os.path.dirname(self.md5_file_path), exist_ok=True)
                
                # Sauvegarde atomique avec fichier temporaire
                temp_file = f"{self.md5_file_path}.tmp"
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(self.md5_data, f, indent=2, ensure_ascii=False)
                
                # Remplacer le fichier original
                os.replace(temp_file, self.md5_file_path)
                self.logger.debug(f"Données MD5 sauvegardées: {len(self.md5_data)} entrées")
            except Exception as e:
                self.logger.error(f"Erreur lors de la sauvegarde des données MD5: {e}")
    
    def calculate_remote_md5(self, url: str, session: requests.Session, timeout: int = 30) -> Optional[str]:
        """
        Calcule l'empreinte MD5 d'un document distant sans le télécharger complètement
        Utilise des requêtes par chunks pour économiser la mémoire
        """
        try:
            self.logger.debug(f"Calcul MD5 distant pour: {url}")
            
            # Faire une requête HEAD pour vérifier la disponibilité
            head_response = session.head(url, timeout=timeout, allow_redirects=True)
            if head_response.status_code != 200:
                self.logger.warning(f"Document inaccessible (HTTP {head_response.status_code}): {url}")
                return None
            
            # Télécharger par chunks pour calculer le MD5
            response = session.get(url, timeout=timeout, stream=True)
            response.raise_for_status()
            
            md5_hash = hashlib.md5()
            chunk_count = 0
            
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    md5_hash.update(chunk)
                    chunk_count += 1
                    
                    # Limite de sécurité pour éviter les fichiers trop volumineux
                    if chunk_count > 6400:  # ~50MB max (8192 * 6400)
                        self.logger.warning(f"Fichier trop volumineux pour calcul MD5: {url}")
                        return None
            
            md5_value = md5_hash.hexdigest()
            self.logger.debug(f"MD5 calculé: {md5_value} pour {url}")
            return md5_value
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Erreur réseau lors du calcul MD5 pour {url}: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Erreur lors du calcul MD5 pour {url}: {e}")
            return None
    
    def calculate_local_md5(self, file_path: str) -> Optional[str]:
        """Calcule l'empreinte MD5 d'un fichier local"""
        try:
            md5_hash = hashlib.md5()
            with open(file_path, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    md5_hash.update(chunk)
            return md5_hash.hexdigest()
        except Exception as e:
            self.logger.error(f"Erreur lors du calcul MD5 local pour {file_path}: {e}")
            return None
    
    def should_download(self, url: str, local_path: str, session: requests.Session, timeout: int = 30) -> Dict[str, Any]:
        """
        Détermine si un document doit être téléchargé en comparant les MD5
        
        Returns:
            Dict avec 'should_download' (bool), 'reason' (str), 'remote_md5' (str), 'local_md5' (str)
        """
        with self.lock:
            result = {
                'should_download': True,
                'reason': 'nouveau_document',
                'remote_md5': None,
                'local_md5': None
            }
            
            try:
                # Vérifier si le fichier local existe
                if not os.path.exists(local_path):
                    result['reason'] = 'fichier_local_inexistant'
                    return result
                
                # Calculer le MD5 du fichier local
                local_md5 = self.calculate_local_md5(local_path)
                if not local_md5:
                    result['reason'] = 'erreur_md5_local'
                    return result
                
                result['local_md5'] = local_md5
                
                # Calculer le MD5 du document distant
                remote_md5 = self.calculate_remote_md5(url, session, timeout)
                if not remote_md5:
                    # Si on ne peut pas calculer le MD5 distant, on garde le fichier local
                    result['should_download'] = False
                    result['reason'] = 'erreur_md5_distant_garde_local'
                    return result
                
                result['remote_md5'] = remote_md5
                
                # Comparer les MD5
                if local_md5 == remote_md5:
                    result['should_download'] = False
                    result['reason'] = 'md5_identique'
                    self.logger.info(f"MD5 identique, téléchargement ignoré: {url}")
                else:
                    result['reason'] = 'md5_different'
                    self.logger.info(f"MD5 différent, téléchargement nécessaire: {url}")
                
                return result
                
            except Exception as e:
                self.logger.error(f"Erreur lors de la vérification MD5 pour {url}: {e}")
                result['reason'] = f'erreur_verification: {str(e)}'
                return result
    
    def update_md5_record(self, url: str, local_path: str, md5_value: str, document_info: Dict[str, Any]):
        """Met à jour l'enregistrement MD5 pour un document"""
        with self.lock:
            try:
                self.md5_data[url] = {
                    'md5': md5_value,
                    'local_path': local_path,
                    'last_updated': datetime.now().isoformat(),
                    'file_size': os.path.getsize(local_path) if os.path.exists(local_path) else 0,
                    'document_info': document_info
                }
                self.save_md5_data()
                self.logger.debug(f"Enregistrement MD5 mis à jour pour: {url}")
            except Exception as e:
                self.logger.error(f"Erreur lors de la mise à jour MD5 pour {url}: {e}")
    
    def get_md5_record(self, url: str) -> Optional[Dict[str, Any]]:
        """Récupère l'enregistrement MD5 pour une URL"""
        with self.lock:
            return self.md5_data.get(url)

class DocumentDownloader:
    """
    Téléchargeur de documents juridiques avec gestion avancée et vérification MD5
    """
    
    def __init__(self, config: Dict[str, Any]):
        """Initialise le téléchargeur avec la configuration"""
        self.base_download_dir = config.get("download_dir", "downloads")
        self.max_workers = config.get("max_workers", 3)
        self.delay_between_downloads = config.get("delay", 0.5)
        self.timeout = config.get("timeout", 30)
        self.max_retries = config.get("max_retries", 3)
        self.max_file_size = config.get("max_file_size_mb", 50) * 1024 * 1024  # En bytes
        self.resume_downloads = config.get("resume", True)
        self.organize_by_type = config.get("organize_by_type", True)
        self.organize_by_category = config.get("organize_by_category", True)
        self.organize_by_date = config.get("organize_by_date", False)
        self.enable_md5_verification = config.get("enable_md5_verification", True)
        
        # Statistiques
        self.stats = {
            "total_documents": 0,
            "downloaded": 0,
            "skipped": 0,
            "errors": 0,
            "total_size_mb": 0,
            "start_time": time.time(),
            "download_progress": [],
            "md5_checks": 0,
            "md5_identical": 0,
            "md5_different": 0,
            "md5_errors": 0
        }
        
        # Session HTTP avec retry
        self.session = requests.Session()
        retry_strategy = Retry(
            total=self.max_retries,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
        # Headers pour éviter les blocages
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        })
        
        # Configuration du logging
        self.setup_logging()
        
        # Créer les répertoires de base
        self.create_directory_structure()
        
        # Fichier de progression
        self.progress_file = os.path.join(self.base_download_dir, "download_progress.json")
        self.downloaded_files = self.load_progress()
        
        # Gestionnaire MD5
        if self.enable_md5_verification:
            md5_file = os.path.join(self.base_download_dir, "md5_checksums.json")
            self.md5_manager = MD5Manager(md5_file)
            self.log("🔐 Vérification MD5 activée")
        else:
            self.md5_manager = None
            self.log("⚠️ Vérification MD5 désactivée")
    
    def create_directory_structure(self):
        """Crée la structure de répertoires pour organiser les téléchargements"""
        base_path = Path(self.base_download_dir)
        base_path.mkdir(exist_ok=True)
        
        if not self.organize_by_category and self.organize_by_type:
            for doc_type in ["pdf", "doc", "docx", "txt", "html", "rtf", "odt", "autres"]:
                (base_path / doc_type).mkdir(exist_ok=True)
    
    def load_progress(self) -> Dict[str, Dict]:
        """Charge la progression des téléchargements précédents"""
        if os.path.exists(self.progress_file):
            try:
                with open(self.progress_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                self.log(f"⚠️ Erreur lors du chargement de la progression: {e}")
        return {}
    
    def save_progress(self):
        """Sauvegarde la progression des téléchargements"""
        try:
            with open(self.progress_file, 'w', encoding='utf-8') as f:
                json.dump(self.downloaded_files, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.log(f"⚠️ Erreur lors de la sauvegarde: {e}")
    
    def setup_logging(self):
        """Configure le système de logging"""
        # Logger principal
        self.logger = logging.getLogger(f"{__name__}.DocumentDownloader")
        
        # Configuration du niveau de log
        log_level = logging.INFO
        self.logger.setLevel(log_level)
        
        # Éviter la duplication des handlers
        if not self.logger.handlers:
            # Handler pour la console
            console_handler = logging.StreamHandler()
            console_handler.setLevel(log_level)
            
            # Format des messages
            formatter = logging.Formatter(
                '[%(asctime)s] %(levelname)s - %(name)s - %(message)s',
                datefmt='%H:%M:%S'
            )
            console_handler.setFormatter(formatter)
            
            self.logger.addHandler(console_handler)
            
            # Handler pour fichier (optionnel)
            log_file = os.path.join(self.base_download_dir, "downloader.log")
            try:
                os.makedirs(os.path.dirname(log_file), exist_ok=True)
                file_handler = logging.FileHandler(log_file, encoding='utf-8')
                file_handler.setLevel(logging.DEBUG)
                file_handler.setFormatter(formatter)
                self.logger.addHandler(file_handler)
            except Exception:
                pass  # Ignorer si on ne peut pas créer le fichier de log
    
    def log(self, message: str):
        """Affiche un message avec timestamp (compatibilité)"""
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] {message}")
        
        # Log aussi via le logger si disponible
        if hasattr(self, 'logger'):
            self.logger.info(message)
    
    def get_file_extension(self, url: str, content_type: str = None) -> str:
        """Détermine l'extension du fichier à partir de l'URL ou du content-type"""
        # D'abord essayer depuis l'URL
        parsed_url = urlparse(url)
        path = unquote(parsed_url.path)
        
        # Chercher une extension dans l'URL
        ext_match = re.search(r'\.([a-zA-Z0-9]+)(?:\?|$)', path)
        if ext_match:
            return ext_match.group(1).lower()
        
        # Essayer depuis le content-type
        if content_type:
            content_type_map = {
                'application/pdf': 'pdf',
                'application/msword': 'doc',
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
                'text/plain': 'txt',
                'text/html': 'html',
                'application/rtf': 'rtf'
            }
            return content_type_map.get(content_type.split(';')[0], 'bin')
        
        return 'bin'
    
    def generate_filename(self, document: Dict[str, Any], extension: str) -> str:
        """Génère un nom de fichier sécurisé"""
        title = document.get('title', 'document_sans_titre')
        
        # Nettoyer le titre pour en faire un nom de fichier valide
        filename = re.sub(r'[<>:"/\\|?*]', '_', title)
        filename = re.sub(r'\s+', '_', filename)
        filename = filename[:100]  # Limiter la longueur
        
        # Ajouter un hash de l'URL pour éviter les doublons
        url_hash = hashlib.md5(document['url'].encode()).hexdigest()[:8]
        
        return f"{filename}_{url_hash}.{extension}"
    
    @staticmethod
    def sanitize_segment(segment: str) -> str:
        cleaned = re.sub(r"[<>:\"/\\|?*]", "_", segment)
        cleaned = re.sub(r"\s+", "_", cleaned).strip("_")
        return cleaned[:80] or "inconnu"

    def get_download_path(self, document: Dict[str, Any], filename: str) -> Path:
        """Détermine le chemin de téléchargement selon l'organisation choisie"""
        base_path = Path(self.base_download_dir)
        
        parts: List[str] = []

        if self.organize_by_category:
            category = document.get("category", "Autres")
            parts.append(self.sanitize_segment(category))
            section = document.get("section")
            if section:
                parts.append(self.sanitize_segment(section))

        if self.organize_by_type:
            doc_type = document.get('doc_type', document.get('type', 'autres'))
            if doc_type not in ['pdf', 'doc', 'docx', 'txt', 'html', 'rtf', 'odt']:
                doc_type = 'autres'
            parts.append(self.sanitize_segment(doc_type))

        target_dir = base_path.joinpath(*parts) if parts else base_path
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / filename
    
    def download_document(self, document: Dict[str, Any]) -> Dict[str, Any]:
        """Télécharge un document individuel avec vérification MD5"""
        url = document['url']
        
        try:
            # Déterminer l'extension et le nom de fichier d'abord
            head_response = self.session.head(url, timeout=self.timeout, allow_redirects=True)
            if head_response.status_code != 200:
                self.stats["errors"] += 1
                return {"status": "error", "reason": f"HTTP {head_response.status_code}", "url": url}
            
            content_type = head_response.headers.get('content-type', '')
            extension = self.get_file_extension(url, content_type)
            filename = self.generate_filename(document, extension)
            local_path = self.get_download_path(document, filename)
            
            # Créer le répertoire parent si nécessaire
            local_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Vérification MD5 si activée
            if self.md5_manager and self.enable_md5_verification:
                self.stats["md5_checks"] += 1
                
                md5_check = self.md5_manager.should_download(url, str(local_path), self.session, self.timeout)
                
                if not md5_check['should_download']:
                    if md5_check['reason'] == 'md5_identique':
                        self.stats["md5_identical"] += 1
                        self.stats["skipped"] += 1
                        self.log(f"🔐 MD5 identique, ignoré: {filename}")
                        return {
                            "status": "skipped", 
                            "reason": "md5_identical", 
                            "url": url,
                            "local_md5": md5_check.get('local_md5'),
                            "remote_md5": md5_check.get('remote_md5')
                        }
                    elif md5_check['reason'] == 'erreur_md5_distant_garde_local':
                        self.stats["md5_errors"] += 1
                        self.stats["skipped"] += 1
                        self.log(f"⚠️ Erreur MD5 distant, fichier local conservé: {filename}")
                        return {
                            "status": "skipped", 
                            "reason": "md5_error_keep_local", 
                            "url": url
                        }
                
                if md5_check['reason'] == 'md5_different':
                    self.stats["md5_different"] += 1
                    self.log(f"🔄 MD5 différent, mise à jour: {filename}")
            
            # Vérification classique si MD5 désactivé
            elif url in self.downloaded_files and self.resume_downloads:
                file_info = self.downloaded_files[url]
                if os.path.exists(file_info.get('local_path', '')):
                    self.stats["skipped"] += 1
                    return {"status": "skipped", "reason": "already_downloaded", "url": url}
            # Vérifier la taille du fichier
            content_length = head_response.headers.get('content-length')
            if content_length and int(content_length) > self.max_file_size:
                self.stats["skipped"] += 1
                return {"status": "skipped", "reason": "file_too_large", "url": url, "size": content_length}
            
            # Télécharger le fichier
            self.log(f"📥 Téléchargement: {filename}")
            response = self.session.get(url, timeout=self.timeout, stream=True)
            response.raise_for_status()
            
            # Sauvegarder le fichier
            with open(local_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            
            # Calculer la taille
            file_size = os.path.getsize(local_path)
            self.stats["total_size_mb"] += file_size / (1024 * 1024)
            
            # Calculer et enregistrer le MD5 après téléchargement si activé
            if self.md5_manager and self.enable_md5_verification:
                local_md5 = self.md5_manager.calculate_local_md5(str(local_path))
                if local_md5:
                    self.md5_manager.update_md5_record(url, str(local_path), local_md5, document)
                    self.log(f"🔐 MD5 enregistré pour {filename}: {local_md5}")
            
            # Enregistrer dans la progression
            self.downloaded_files[url] = {
                "local_path": str(local_path),
                "filename": filename,
                "size_bytes": file_size,
                "content_type": content_type,
                "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "document_info": document
            }
            
            self.stats["downloaded"] += 1
            self.log(f"✅ Téléchargé: {filename} ({file_size/1024:.1f} KB)")
            
            return {"status": "success", "url": url, "local_path": str(local_path), "size": file_size}
            
        except Exception as e:
            self.stats["errors"] += 1
            self.log(f"❌ Erreur pour {url}: {e}")
            return {"status": "error", "reason": str(e), "url": url}
    
    def download_documents(self, documents: List[Dict[str, Any]], max_documents: Optional[int] = None):
        """Télécharge une liste de documents avec parallélisation"""
        self.stats["total_documents"] = len(documents)
        
        if max_documents:
            documents = documents[:max_documents]
            self.log(f"🎯 Limitation à {max_documents} documents")
        
        self.log(f"🚀 Début du téléchargement de {len(documents)} documents")
        self.log(f"📁 Répertoire: {self.base_download_dir}")
        self.log(f"🔧 Workers: {self.max_workers}")
        
        # Téléchargement parallèle
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Soumettre toutes les tâches
            future_to_doc = {
                executor.submit(self.download_document, doc): doc 
                for doc in documents
            }
            
            # Traiter les résultats au fur et à mesure
            for future in as_completed(future_to_doc):
                result = future.result()
                
                # Sauvegarder la progression périodiquement
                if self.stats["downloaded"] % 10 == 0:
                    self.save_progress()
                
                # Délai entre téléchargements
                if self.delay_between_downloads > 0:
                    time.sleep(self.delay_between_downloads)
        
        # Sauvegarde finale
        self.save_progress()
        
        # Statistiques finales
        duration = time.time() - self.stats["start_time"]
        self.log(f"\n🎉 Téléchargement terminé!")
        self.log(f"   ✅ Téléchargés: {self.stats['downloaded']}")
        self.log(f"   ⏭️  Ignorés: {self.stats['skipped']}")
        self.log(f"   ❌ Erreurs: {self.stats['errors']}")
        self.log(f"   💾 Taille totale: {self.stats['total_size_mb']:.1f} MB")
        self.log(f"   ⏱️  Durée: {duration:.1f}s")
        if self.stats["downloaded"] > 0:
            self.log(f"   🚀 Vitesse: {self.stats['downloaded']/duration:.2f} docs/sec")
        
        # Statistiques MD5 si activées
        if self.enable_md5_verification:
            self.log(f"\n🔐 STATISTIQUES MD5:")
            self.log(f"   Vérifications MD5: {self.stats['md5_checks']}")
            self.log(f"   MD5 identiques (ignorés): {self.stats['md5_identical']}")
            self.log(f"   MD5 différents (mis à jour): {self.stats['md5_different']}")
            self.log(f"   Erreurs MD5: {self.stats['md5_errors']}")

        return self.stats

def load_documents_from_crawler_results(results_file: str) -> List[Dict[str, Any]]:
    """Charge les documents depuis les résultats du crawler"""
    try:
        with open(results_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        if isinstance(data, dict) and 'documents' in data:
            return data['documents']
        elif isinstance(data, list):
            return data
        else:
            raise ValueError("Format de fichier non reconnu")
    
    except Exception as e:
        print(f"❌ Erreur lors du chargement de {results_file}: {e}")
        return []

def main():
    """Fonction principale"""
    parser = argparse.ArgumentParser(description="Téléchargeur de documents juridiques")
    
    # Fichier source
    parser.add_argument("--results-file", required=True,
                       help="Fichier JSON contenant les résultats du crawler")
    
    # Configuration de téléchargement
    parser.add_argument("--download-dir", default="downloads",
                       help="Répertoire de téléchargement")
    parser.add_argument("--max-workers", type=int, default=3,
                       help="Nombre de téléchargements parallèles")
    parser.add_argument("--delay", type=float, default=0.5,
                       help="Délai entre téléchargements (secondes)")
    parser.add_argument("--timeout", type=int, default=30,
                       help="Timeout des téléchargements")
    parser.add_argument("--max-retries", type=int, default=3,
                       help="Nombre de tentatives en cas d'erreur")
    
    # Limitations
    parser.add_argument("--max-documents", type=int,
                       help="Nombre maximum de documents à télécharger")
    parser.add_argument("--max-file-size-mb", type=int, default=50,
                       help="Taille maximale des fichiers (MB)")
    
    # Organisation
    parser.add_argument("--organize-by-type", dest="organize_by_type", action="store_true",
                       help="Organiser par type de document (défaut)")
    parser.add_argument("--no-organize-by-type", dest="organize_by_type", action="store_false",
                       help="Ne pas créer de sous-dossiers par type")
    parser.set_defaults(organize_by_type=True)

    parser.add_argument("--organize-by-category", dest="organize_by_category", action="store_true",
                       help="Organiser par catégorie/section (défaut)")
    parser.add_argument("--no-organize-by-category", dest="organize_by_category", action="store_false",
                       help="Ne pas créer de sous-dossiers par catégorie/section")
    parser.set_defaults(organize_by_category=True)

    parser.add_argument("--organize-by-date", action="store_true", default=False,
                       help="Organiser par date")
    
    # Options
    parser.add_argument("--resume", action="store_true", default=True,
                       help="Reprendre les téléchargements interrompus")
    
    args = parser.parse_args()
    
    # Vérifier que le fichier de résultats existe
    if not os.path.exists(args.results_file):
        print(f"❌ Fichier non trouvé: {args.results_file}")
        return
    
    # Charger les documents
    print(f"📂 Chargement des documents depuis: {args.results_file}")
    documents = load_documents_from_crawler_results(args.results_file)
    
    if not documents:
        print("❌ Aucun document trouvé dans le fichier")
        return
    
    print(f"📋 {len(documents)} documents trouvés")
    
    # Configuration du téléchargeur
    config = {
        "download_dir": args.download_dir,
        "max_workers": args.max_workers,
        "delay": args.delay,
        "timeout": args.timeout,
        "max_retries": args.max_retries,
        "max_file_size_mb": args.max_file_size_mb,
        "organize_by_type": args.organize_by_type,
        "organize_by_category": args.organize_by_category,
        "organize_by_date": args.organize_by_date,
        "resume": args.resume,
        "enable_md5_verification": True  # Activer la vérification MD5 par défaut
    }
    
    # Créer et lancer le téléchargeur
    downloader = DocumentDownloader(config)
    downloader.download_documents(documents, args.max_documents)
    
    print(f"\n📁 Documents téléchargés dans: {args.download_dir}")

if __name__ == "__main__":
    main()
