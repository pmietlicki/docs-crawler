import os
import pathlib
import requests
import base64
import time
import asyncio
import aiohttp
import aiofiles
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin, unquote
from PyPDF2 import PdfMerger
import pdfkit
import subprocess
import tempfile
import shutil
import re
import mimetypes
from typing import List, Dict, Set, Optional, Tuple
from pyppeteer import launch

# Configuration de base
username = os.getenv('DOCKTAIL_USERNAME')
password = os.getenv('DOCKTAIL_PASSWORD')
baseURL = 'https://docktail.test.local/'
saveDirectory = './docs'
maxDepth = 10

# Création du dossier de sauvegarde s'il n'existe pas
pathlib.Path(saveDirectory).mkdir(parents=True, exist_ok=True)

# Variables globales pour le suivi des téléchargements
externalLinkCounter = 0
globalQueue = []
directDownloadAttempts = {}
currentPageDir = None
requestMap = {}
currentIsExternalLink = False
currentExternalLinkIndex = None
resourceCache = {}

def cacheResource(url: str, buffer: bytes, contentType: str):
    resourceCache[url] = {
        'buffer': buffer,
        'contentType': contentType,
        'timestamp': time.time()
    }

def getCachedResource(url: str):
    cached = resourceCache.get(url)
    if cached and time.time() - cached['timestamp'] < 3600:  # Cache d'1h
        return cached
    return None

def getPageDirectoryFromUrl(url: str) -> str:
    urlObj = urlparse(url)
    parts = [p for p in urlObj.path.split('/') if p]
    parts = [unquote(p) for p in parts]
    dirPath = os.path.join(saveDirectory, *parts)
    os.makedirs(dirPath, exist_ok=True)
    return dirPath

def normalizeUrl(url: str) -> Optional[str]:
    try:
        u = urlparse(url)
        u = u._replace(fragment='', query='')
        return u.geturl()
    except:
        return None

def shouldDownloadResource(contentType: str, buffer: bytes, url: str = '') -> bool:
    if not contentType:
        return False
    contentType = contentType.lower()
    lowerUrl = url.lower()
    if (lowerUrl.endswith(('.ttf', '.otf', '.woff', '.woff2', '.svg'))):
        return False
    if contentType.startswith('text/html') and buffer and len(buffer) < 1024:
        return False
    if any(ct in contentType for ct in ['pdf', 'msword', 'openxmlformats', 'ms-excel', 'ms-powerpoint']) or contentType.startswith('text/html'):
        return True
    return False

def isDocumentUrl(url: str) -> bool:
    docExtensions = ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.xlsm']
    pathname = urlparse(url).path.lower()
    return any(pathname.endswith(ext) for ext in docExtensions)


def getLocalFileName(pageDir: str, url: str, contentType: str = '', isExternalLink: bool = False, externalLinkIndex: Optional[int] = None) -> str:
    urlObj = urlparse(url)
    filename = urlObj.path.split('/')[-1] or 'file'
    filename = filename.replace('%2F', '_')
    filename = unquote(filename) or 'file'
    if not os.path.splitext(filename)[1]:
        if 'pdf' in contentType:
            filename += '.pdf'
        elif any(ct in contentType for ct in ['msword', 'ms-word']):
            filename += '.doc'
        elif 'openxmlformats' in contentType:
            filename += '.docx'
        elif 'ms-excel' in contentType:
            filename += '.xls'
        elif 'ms-powerpoint' in contentType:
            filename += '.ppt'
        elif contentType.startswith('text/html'):
            filename += '.html'
    if isExternalLink and externalLinkIndex is not None:
        filename = f'lien{externalLinkIndex}-{filename}'
    finalPath = os.path.join(pageDir, filename)
    os.makedirs(os.path.dirname(finalPath), exist_ok=True)
    return finalPath

def getLocalResourceFilePath(pageDir: str, url: str, contentType: str = '', isExternalLink: bool = False, externalLinkIndex: Optional[int] = None) -> str:
    return getLocalFileName(pageDir, url, contentType, isExternalLink, externalLinkIndex)

async def embedImagesInHtml(html: str, pageDir: str, baseUrl: str, page) -> str:
    soup = BeautifulSoup(html, 'html.parser')
    imgElements = soup.find_all('img')
    print(f'Nombre d\'images trouvées: {len(imgElements)}')

    def getMimeType(ext: str) -> str:
        mimeTypes = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.svg': 'image/svg+xml',
            '.webp': 'image/webp',
            '.bmp': 'image/bmp'
        }
        return mimeTypes.get(ext.lower(), 'image/png')

    async def processImage(el):
        img = el
        src = img.get('src')
        if not src or src.startswith('data:'):
            return
        absoluteUrl = src if src.startswith('http') else urljoin(baseUrl, src)
        print(f'Téléchargement de l\'image: {absoluteUrl}')
        cookies = await page.cookies()
        cookieHeader = '; '.join([f'{c["name"]}={c["value"]}' for c in cookies])
        headers = {
            'Authorization': 'Basic ' + base64.b64encode(f'{username}:{password}'.encode()).decode(),
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Cookie': cookieHeader
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(absoluteUrl, headers=headers, timeout=5) as response:
                if response.status != 200:
                    print(f'Erreur téléchargement (status {response.status}) : {absoluteUrl}')
                    return
                imgData = await response.read()
                mime = response.headers.get('content-type', getMimeType(os.path.splitext(src)[1]))
                if len(imgData) > 10 * 1024 * 1024:  # Limite 10MB
                    print(f'Image trop volumineuse (>10MB): {src}')
                    return
                base64Data = base64.b64encode(imgData).decode()
                dataUri = f'data:{mime};base64,{base64Data}'
                img['src'] = dataUri
                print('Image convertie en base64 avec succès.')

    concurrencyLimit = 5
    for i in range(0, len(imgElements), concurrencyLimit):
        chunk = imgElements[i:i + concurrencyLimit]
        await asyncio.gather(*[processImage(el) for el in chunk])

    return str(soup)

async def saveHtml(pageDir: str, url: str, html: str, isExternalLink: bool = False, externalLinkIndex: Optional[int] = None, page=None):
    contentType = 'text/html'
    buffer = html.encode('utf-8')
    if not shouldDownloadResource(contentType, buffer, url):
        print(f'HTML trop petit ou non valide, non sauvegardé: {url}')
        return
    injectionScript = '<script>window.location.replace = function(url){console.log("Redirection désactivée");};</script>'
    html = html.replace('<head>', f'<head>{injectionScript}', 1)
    html = await embedImagesInHtml(html, pageDir, url, page)
    filename = 'lien{}-index.html'.format(externalLinkIndex) if isExternalLink and externalLinkIndex is not None else 'index.html'
    filePath = os.path.join(pageDir, filename)
    if not os.path.exists(filePath):
        async with aiofiles.open(filePath, 'w', encoding='utf-8') as f:
            await f.write(html)
        print(f'Page sauvegardée: {url} -> {filePath}')

async def downloadDirectWithAuth(pageDir: str, url: str, isExternalLink: bool = False, externalLinkIndex: Optional[int] = None) -> bool:
    cached = getCachedResource(url)
    if cached:
        print(f'Utilisation de la ressource en cache pour: {url}')
        filePath = getLocalResourceFilePath(pageDir, url, cached['contentType'], isExternalLink, externalLinkIndex)
        if not os.path.exists(filePath):
            async with aiofiles.open(filePath, 'wb') as f:
                await f.write(cached['buffer'])
            print(f'Document récupéré du cache: {url} -> {filePath}')
        return True
    maxRetries = 3
    retryDelay = 2
    for attempt in range(1, maxRetries + 1):
        try:
            authHeader = 'Basic ' + base64.b64encode(f'{username}:{password}'.encode()).decode()
            headers = {
                'Authorization': authHeader,
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=10) as res:
                    if res.status != 200:
                        raise Exception(f'HTTP {res.status}')
                    buffer = await res.read()
                    contentType = res.headers.get('content-type', '')
                    if not shouldDownloadResource(contentType, buffer, url):
                        print(f'Ressource non conforme ou vide: {url}')
                        return False
                    cacheResource(url, buffer, contentType)
                    filePath = getLocalResourceFilePath(pageDir, url, contentType, isExternalLink, externalLinkIndex)
                    if not os.path.exists(filePath):
                        async with aiofiles.open(filePath, 'wb') as f:
                            await f.write(buffer)
                        print(f'Document téléchargé et mis en cache: {url} -> {filePath}')
                    return True
        except Exception as error:
            if attempt == maxRetries:
                raise error
            print(f'Tentative {attempt}/{maxRetries} échouée, nouvelle tentative dans {retryDelay}s')
            await asyncio.sleep(retryDelay)
    return False

async def downloadDocument(page, pageDir: str, url: str, isExternalLink: bool = False, externalLinkIndex: Optional[int] = None):
    print(f'Téléchargement du document: {url}')
    try:
        authHeader = 'Basic ' + base64.b64encode(f'{username}:{password}'.encode()).decode()
        headers = {
            'Authorization': authHeader,
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    print(f'Échec du téléchargement du document: {url}, Status: {response.status}')
                    return
                buffer = await response.read()
                contentType = response.headers.get('content-type', '')
                if not shouldDownloadResource(contentType, buffer, url):
                    print(f'Ressource non conforme ou vide: {url}')
                    return
                filePath = getLocalFileName(pageDir, url, contentType, isExternalLink, externalLinkIndex)
                if not os.path.exists(filePath):
                    async with aiofiles.open(filePath, 'wb') as f:
                        await f.write(buffer)
                    print(f'Document téléchargé: {url} -> {filePath}')
                else:
                    print(f'Fichier déjà existant, non réécrit: {filePath}')
    except Exception as e:
        print(f'Erreur téléchargement doc {url}:', e)

async def extractAllLinks(page, currentUrl: str) -> List[str]:
    try:
        return await page.evaluate('''(base) => {
            const baseObj = new URL(base);
            const allLinks = new Set();
            function absoluteUrl(href) {
                try {
                    return new URL(href, baseObj.href).href;
                } catch (e) {
                    return null;
                }
            }
            document.querySelectorAll('[routerLink]').forEach(el => {
                const rl = el.getAttribute('routerlink');
                if (rl) {
                    const abs = absoluteUrl(rl);
                    if (abs && abs.startsWith(baseObj.origin)) {
                        allLinks.add(abs);
                    }
                }
            });
            document.querySelectorAll('a[href]').forEach(a => {
                const href = a.getAttribute('href');
                const abs = absoluteUrl(href);
                if (abs && abs.startsWith(baseObj.origin)) {
                    allLinks.add(abs);
                }
            });
            return Array.from(allLinks);
        }''', currentUrl)
    except Exception as error:
        print('Erreur lors de l\'extraction des liens de la page :', error)
        return []

async def extractDocumentsFromPage(page) -> List[str]:
    try:
        return await page.evaluate('''() => {
            const docs = [];
            const exts = ['.xls', '.xlsx', '.xlsm', '.pdf', '.doc', '.docx', '.ppt', '.pptx', '.sql'];
            document.querySelectorAll('a[href]').forEach(a => {
                const href = a.href;
                if (exts.some(ext => href.toLowerCase().endsWith(ext))) {
                    docs.push(href);
                }
            });
            return docs;
        }''')
    except Exception as error:
        print('Erreur lors de l\'extraction des documents de la page :', error)
        return []

async def setupInterception(page):
    async def onRequest(request):
        requestMap[request] = {
            'pageDir': currentPageDir,
            'isExternalLink': currentIsExternalLink,
            'externalLinkIndex': currentExternalLinkIndex
        }

    async def onResponse(response):
        url = response.url
        lowerUrl = url.lower()
        status = response.status
        request = response.request
        resourceType = request.resourceType
        if (lowerUrl.endswith(('.ttf', '.otf', '.woff', '.woff2', '.svg')) or
                resourceType in ['image', 'font', 'stylesheet', 'script']):
            return
        if 300 <= status < 400:
            print(f'Ressource en redirection, pas de body: {url}')
            return
        headers = response.headers
        contentType = headers.get('content-type', '').lower()
        if not contentType:
            print(f'Réponse sans type de contenu détectée: {url}')
            return
        try:
            raw = await response.buffer()
            # ─── normalisation ───────────────────────────────────────────────────
            if isinstance(raw, str):
                raw = raw.encode()                  # utf-8 par défaut
            buffer = raw
        except Exception as err:
            print(f'Skipping buffer due to error retrieving body for {url}: {err}')
            return
        if not shouldDownloadResource(contentType, buffer, url):
            return
        


        context = requestMap.pop(request, {
            'pageDir': currentPageDir or saveDirectory,
            'isExternalLink': False,
            'externalLinkIndex': None
        })
        if len(buffer) == 0 and 'pdf' in contentType:
            print(f'Document intercepté vide: {url}. Tentative d\'accès direct avec authentification...')
            await downloadDirectWithAuth(context['pageDir'], url, context['isExternalLink'], context['externalLinkIndex'])
            return
        filePath = getLocalResourceFilePath(context['pageDir'], url, contentType, context['isExternalLink'], context['externalLinkIndex'])
        if os.path.exists(filePath):
            print(f'Fichier déjà existant, non réécrit: {filePath}')
            return
        async with aiofiles.open(filePath, 'wb') as f:
            await f.write(buffer)  # Assurez-vous que buffer est de type bytes
        print(f'Document téléchargé (via interception): {url} -> {filePath}')

    page.on('request', lambda req: asyncio.ensure_future(onRequest(req)))
    page.on('response', lambda res: asyncio.ensure_future(onResponse(res)))

async def autoScroll(page):
    try:
        await page.evaluate('''async () => {
            await new Promise((resolve, reject) => {
                let totalHeight = 0;
                const distance = 100;
                const maxScrollAttempts = 50;
                let attempts = 0;
                const timer = setInterval(() => {
                    const scrollHeight = document.body.scrollHeight;
                    window.scrollBy(0, distance);
                    totalHeight += distance;
                    attempts++;
                    if (totalHeight >= scrollHeight || attempts >= maxScrollAttempts) {
                        clearInterval(timer);
                        if (totalHeight < scrollHeight) {
                            console.warn('Scroll incomplet, page trop longue ou problème détecté.');
                        }
                        resolve();
                    }
                }, 200);
            });
        }''')
    except Exception as error:
        print('Erreur lors du défilement automatique de la page :', error)

async def clickAllDocumentButtons(page, pageDir: str, visitedUrls: Set[str], depth: int, maxDepthVal: int):
    await autoScroll(page)
    selector = 'button[routerlink], a[routerlink]'
    origin_url = page.url
    router_links = await page.evaluate(
        '''(sel) => {
            return Array.from(document.querySelectorAll(sel))
                        .map(el => el.getAttribute('routerlink'))
                        .filter(href => href);
        }''',
        selector
    )
    for i, href in enumerate(router_links):
        target_url = urljoin(origin_url, href)
        newPageDir = (
            pageDir if "/doc/" in target_url
            else getPageDirectoryFromUrl(target_url)
        )
        global currentPageDir, currentIsExternalLink, currentExternalLinkIndex
        currentPageDir = newPageDir
        currentIsExternalLink = False
        currentExternalLinkIndex = None

        print(f"Clic routerlink {i+1}/{len(router_links)} → {target_url}")
        try:
            # navigation directe
            await page.goto(target_url, {'waitUntil': 'networkidle2'})
            await asyncio.sleep(2)
        except Exception as e:
            print(f"Erreur navigation routerlink {href}: {e}")
            # retour « propre »
            await page.goto(origin_url, {'waitUntil': 'networkidle2'})
            await asyncio.sleep(2)
            continue

        # exploration récursive sur la nouvelle page
        await explorePageRecursively(
            page,
            newPageDir,
            visitedUrls,
            depth + 1,
            maxDepthVal,
            False, None, pageDir
        )

        # retour à la page d’origine
        await page.goto(origin_url, {'waitUntil': 'networkidle2'})
        await asyncio.sleep(2)
        currentPageDir = pageDir
        currentIsExternalLink = False
        currentExternalLinkIndex = None

    # exploration finale sur la page courante
    await explorePageRecursively(
        page, pageDir, visitedUrls, depth, maxDepthVal, False, None, pageDir
    )


async def saveWikiContentFrames(page, basePageDir: str, isExternalLink: bool = False, externalLinkIndex: Optional[int] = None):
    frames = page.frames
    savedFrames = set()
    for frame in frames:
        try:
            frameUrl = frame.url
            wikiElem = await frame.querySelector('.wiki-content')
            if wikiElem and frameUrl not in savedFrames:
                html = await frame.content()
                html = await embedImagesInHtml(html, basePageDir, frameUrl, page)
                sanitizedUrlPart = re.sub(r'[^\w.-]+', '_', frameUrl)
                filename = f'lien{externalLinkIndex}-frame-{sanitizedUrlPart}.html' if isExternalLink and externalLinkIndex is not None else f'frame-{sanitizedUrlPart}.html'
                filePath = os.path.join(basePageDir, filename)
                async with aiofiles.open(filePath, 'w', encoding='utf-8') as f:
                    await f.write(html)
                print(f'Frame wiki-content sauvegardée: {filePath}')
                savedFrames.add(frameUrl)
                links = await frame.evaluate('''() => {
                    return Array.from(document.querySelectorAll('a[href]')).map(link => link.getAttribute('href'));
                }''')
                for link in links:
                    if link.startswith('attach/'):
                        absoluteUrl = urljoin(frame.url, link)
                        print(f'Lien trouvé dans la frame : {absoluteUrl}')
                        await downloadDirectWithAuth(basePageDir, absoluteUrl)
        except Exception as err:
            print(f'Erreur lors de la sauvegarde de la frame ou du téléchargement des fichiers liés : {err}')

async def explorePageRecursively(page, pageDir: str, visitedUrls: Set[str], depth: int = 0, maxDepthVal: int = 5, isExternalLink: bool = False, externalLinkIndex: Optional[int] = None, basePageDir: Optional[str] = None):
    if depth > maxDepthVal:
        return
    currentUrl = page.url
    if normalizeUrl(currentUrl) in visitedUrls:
        print(f'Page déjà visitée : {currentUrl}')
        return
    visitedUrls.add(normalizeUrl(currentUrl))
    print(f'Exploration de la page : {currentUrl} (profondeur : {depth})')
    global currentPageDir, currentIsExternalLink, currentExternalLinkIndex
    currentPageDir = pageDir
    currentIsExternalLink = isExternalLink
    currentExternalLinkIndex = externalLinkIndex
    htmlContent = await page.content()
    await saveHtml(pageDir, currentUrl, htmlContent, currentIsExternalLink, currentExternalLinkIndex, page)
    await saveWikiContentFrames(page, pageDir, currentIsExternalLink, currentExternalLinkIndex)
    docs = await extractDocumentsFromPage(page)
    for docUrl in docs:
        await downloadDocument(page, pageDir, docUrl, currentIsExternalLink, currentExternalLinkIndex)

    async def clickAndExplore(selector: str, label: str):
        elements = await page.querySelectorAll(selector)
        for i in range(len(elements)):
            elements = await page.querySelectorAll(selector)
            if i >= len(elements):
                continue

            # 1️⃣ calculer la future URL sans la charger
            target_href = await page.evaluate('(el) => el.getAttribute("routerlink") || el.getAttribute("href")', elements[i])
            target_url  = urljoin(page.url, target_href)

            # 2️⃣ déterminer le dossier cible
            new_page_dir = pageDir if '/doc/' in target_url else getPageDirectoryFromUrl(target_url)

            # 3️⃣ préparer le contexte AVANT de lancer la requête réseau
            new_is_external = (label == 'fa-external-link-alt')
            new_ext_index   = None
            if new_is_external:
                global externalLinkCounter
                externalLinkCounter += 1
                new_ext_index = externalLinkCounter

            currentPageDir        = new_page_dir
            currentIsExternalLink = new_is_external
            currentExternalLinkIndex = new_ext_index

            print(f'Clic sur {label} {i+1}/{len(elements)} → {new_page_dir}')

            # 4️⃣ navigation (les requêtes auront maintenant le bon contexte)
            await elements[i].click()
            await asyncio.sleep(3)

            await explorePageRecursively(
                page, new_page_dir, visitedUrls, depth+1, maxDepthVal,
                new_is_external, new_ext_index, basePageDir or pageDir
            )

            # retour à l’écran précédent
            await page.goto(currentUrl, {'waitUntil': 'networkidle2'})
            currentPageDir = pageDir
            currentIsExternalLink = isExternalLink
            currentExternalLinkIndex = externalLinkIndex


    await clickAndExplore('i.fa-eye', 'fa-eye')
    links = await extractAllLinks(page, currentUrl)
    for l in links:
        normalized = normalizeUrl(l)
        if normalized not in visitedUrls:
            targetPageDir = basePageDir or pageDir if '/doc/' in l else getPageDirectoryFromUrl(l)
            if isDocumentUrl(l):
                print(f'Téléchargement direct du document: {l}')
                try:
                    buffer, contentType = await page.evaluate('''async (downloadUrl) => {
                        const response = await fetch(downloadUrl, { credentials: 'include' });
                        if (!response.ok) {
                            return { buffer: null, contentType: '' };
                        }
                        const arrBuf = await response.arrayBuffer();
                        const contentType = response.headers.get('content-type') || '';
                        return { buffer: Array.from(new Uint8Array(arrBuf)), contentType };
                    }''', l)
                    if not buffer:
                        print(f'Echec du téléchargement du document: {l}')
                        continue
                    buff = bytes(buffer)
                    if not shouldDownloadResource(contentType, buff, l):
                        print(f'Type de ressource non conforme ou vide: {l}')
                        continue
                    filePath = getLocalResourceFilePath(targetPageDir, l, contentType, currentIsExternalLink, currentExternalLinkIndex)
                    if os.path.exists(filePath):
                        print(f'Fichier déjà existant, non réécrit: {filePath}')
                        continue
                    async with aiofiles.open(filePath, 'wb') as f:
                        await f.write(buff)
                    print(f'Document téléchargé: {l} -> {filePath}')
                except Exception as e:
                    print(f'Erreur téléchargement doc {l}:', e)
            else:
                globalQueue.append({'url': l, 'depth': depth + 1, 'pageDir': targetPageDir})
    print(f'Exploration terminée pour : {currentUrl}')

async def crawl(startUrl: str, maxDepth: int):
    browser = await launch({'executablePath': '/usr/bin/chromium-browser', 'headless': True, 'args':['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage'], 'timeout': 60000})
    page = await browser.newPage()
    await page.evaluateOnNewDocument(r'''
    (() => {
    const origPush = history.pushState;
    history.pushState = function(state, title, url) {
        // stocke la nouvelle route
        window.__lastPushedUrl = url;
        return origPush.apply(this, arguments);
    };
    // initialisation
    window.__lastPushedUrl = null;
    // idem pour les retours arrière
    window.addEventListener('popstate', () => {
        window.__lastPushedUrl = location.href;
    });
    })();
    ''')
    await page.authenticate({'username': username, 'password': password})
    global currentPageDir, currentIsExternalLink, currentExternalLinkIndex
    currentPageDir = getPageDirectoryFromUrl(startUrl)
    currentIsExternalLink = False
    currentExternalLinkIndex = None
    await setupInterception(page)  # Utilisez await ici
    visited = set()
    globalQueue.append({'url': startUrl, 'depth': 0, 'pageDir': currentPageDir})
    while globalQueue:
        item = globalQueue.pop(0)
        url, depth, pageDir = item['url'], item['depth'], item['pageDir']
        if depth > maxDepth:
            continue
        if normalizeUrl(url) in visited:
            continue
        currentPageDir = pageDir
        currentIsExternalLink = False
        currentExternalLinkIndex = None
        print(f'Visite de la page: {url} (profondeur: {depth})')
        try:
            await page.goto(url, {'waitUntil': 'networkidle2'})
            await asyncio.sleep(5)
        except Exception as e:
            print(f'Erreur chargement page {url}:', e)
            visited.add(normalizeUrl(url))
            continue
        await clickAllDocumentButtons(page, pageDir, visited, depth, maxDepth)
        visited.add(normalizeUrl(url))
    await browser.close()
    print('Crawl terminé !')


async def convertHtmlToPdf(htmlFilePath: str, pdfFilePath: str):
    options = {
        'format': 'A4',
        'encoding': 'UTF-8',
        'print-media-type': None,
        'quiet': ''
    }
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, pdfkit.from_file, htmlFilePath, pdfFilePath, options)
    print(f'Converti {htmlFilePath} en {pdfFilePath}')

async def getFilesRecursive(dir: str) -> List[str]:
    results = []
    for entry in os.scandir(dir):
        if entry.is_dir():
            results.extend(await getFilesRecursive(entry.path))
        else:
            results.append(entry.path)
    return results

def isValidPdf(filePath: str) -> bool:
    try:
        with open(filePath, 'rb') as f:
            header = f.read(5)
            return header == b'%PDF-'
    except:
        return False

async def collectPdfFilesInDir(dir: str) -> List[str]:
    allFiles = await getFilesRecursive(dir)
    return [file for file in allFiles if file.lower().endswith('.pdf') and isValidPdf(file)]

async def mergePdfForDirectoryRecursively(dirPath: str):
    pdfFiles = await collectPdfFilesInDir(dirPath)
    if not pdfFiles:
        print(f'Aucun PDF trouvé dans {dirPath}')
        return
    merger = PdfMerger()
    for pdf in pdfFiles:
        print(f'Ajout du PDF : {pdf}')
        merger.append(pdf)
    folderName = os.path.basename(dirPath)
    parentDir = os.path.dirname(dirPath)
    outputPdf = os.path.join(parentDir, f'{folderName}.pdf')
    with open(outputPdf, 'wb') as f:
        merger.write(f)
    print(f'PDF fusionné créé pour {dirPath} : {outputPdf}')

async def convertOfficeFileToPdf(filePath: str, pdfFilePath: str):
    if not os.path.exists(filePath):
        print(f'Fichier source introuvable: {filePath}')
        return
    outputDir = os.path.dirname(pdfFilePath)
    command = 'libreoffice'
    args = [
        '--headless',
        '--invisible',
        '--nologo',
        '--nodefault',
        '--nolockcheck',
        '--convert-to', 'pdf',
        filePath,
        '--outdir', outputDir
    ]
    print(f'Conversion Office: Exécution de la commande: {command} {" ".join(args)}')
    process = await asyncio.create_subprocess_exec(command, *args)
    try:
        await asyncio.wait_for(process.wait(), timeout=180)
        if process.returncode == 0:
            print(f'Converti {filePath} en {pdfFilePath}')
        else:
            print(f'Erreur lors de la conversion de {filePath} (code: {process.returncode})')
    except asyncio.TimeoutError:
        print(f'Timeout (180s) atteint pour {filePath}. Kill process.')
        process.kill()
    except Exception as error:
        print(f'Erreur lors du lancement de la commande pour {filePath}:', error)

async def convertAllFilesToPdfInDir(dir: str):
    for entry in os.scandir(dir):
        if entry.is_dir():
            await convertAllFilesToPdfInDir(entry.path)
        elif entry.is_file():
            lowerName = entry.name.lower()
            pdfFilePath = os.path.splitext(entry.path)[0] + '.pdf'
            if lowerName.endswith(('.html', '.htm')):
                print(f'Conversion HTML : {entry.path} -> {pdfFilePath}')
                await convertHtmlToPdf(entry.path, pdfFilePath)
            elif lowerName.endswith(('.doc', '.docx', '.xls', '.xlsx', '.xlsm', '.ppt', '.pptx')):
                print(f'Conversion Office : {entry.path} -> {pdfFilePath}')
                await convertOfficeFileToPdf(entry.path, pdfFilePath)

async def mergeGlobalPdf():
    files = os.listdir(saveDirectory)
    domainPdfFiles = [file for file in files if file.endswith('.pdf') and file != 'global.pdf' and os.path.isfile(os.path.join(saveDirectory, file))]
    if not domainPdfFiles:
        print("Aucun PDF de domaine trouvé pour fusionner en global.pdf")
        return
    merger = PdfMerger()
    for pdf in domainPdfFiles:
        pdfPath = os.path.join(saveDirectory, pdf)
        print(f'Ajout du PDF de domaine: {pdfPath}')
        merger.append(pdfPath)
    outputGlobal = os.path.join(saveDirectory, 'global.pdf')
    with open(outputGlobal, 'wb') as f:
        merger.write(f)
    print(f'PDF global fusionné créé : {outputGlobal}')

async def progressiveMerge(pdfFiles: List[str], outputPdfPath: str, chunkSize: int = 10):
    if not pdfFiles:
        print("Aucun PDF à fusionner.")
        return
    if len(pdfFiles) <= chunkSize:
        merger = PdfMerger()
        for file in pdfFiles:
            print(f'Ajout du PDF : {file}')
            merger.append(file)
        with open(outputPdfPath, 'wb') as f:
            merger.write(f)
        print(f'PDF fusionné créé : {outputPdfPath}')
        return
    tempFiles = []
    for i in range(0, len(pdfFiles), chunkSize):
        chunk = pdfFiles[i:i + chunkSize]
        tempOutput = os.path.join(tempfile.gettempdir(), f'merged_temp_{i}_{time.time()}.pdf')
        merger = PdfMerger()
        for file in chunk:
            print(f'Ajout du PDF (chunk) : {file}')
            merger.append(file)
        with open(tempOutput, 'wb') as f:
            merger.write(f)
        print(f'Groupe fusionné créé : {tempOutput}')
        tempFiles.append(tempOutput)
    await progressiveMerge(tempFiles, outputPdfPath, chunkSize)
    for tempFile in tempFiles:
        os.remove(tempFile)

async def progressiveMergePdfForDirectory(dirPath: str, chunkSize: int = 10):
    pdfFiles = await collectPdfFilesInDir(dirPath)
    if not pdfFiles:
        print(f'Aucun PDF trouvé dans {dirPath}')
        return
    folderName = os.path.basename(dirPath)
    parentDir = os.path.dirname(dirPath)
    outputPdf = os.path.join(parentDir, f'{folderName}.pdf')
    await progressiveMerge(pdfFiles, outputPdf, chunkSize)
    print(f'PDF fusionné créé pour {dirPath} : {outputPdf}')

async def mergeAllPdfInTree(rootDir: str):
    for entry in os.scandir(rootDir):
        if entry.is_dir():
            await mergeAllPdfInTree(entry.path)
            await progressiveMergePdfForDirectory(entry.path)

async def cleanup():
    try:
        tempFiles = await getFilesRecursive(saveDirectory)
        for file in tempFiles:
            if file.endswith('.tmp'):
                os.remove(file)
        resourceCache.clear()
    except Exception as error:
        print('Erreur lors du nettoyage:', error)

async def main():
    await crawl(baseURL, maxDepth)
    print('Crawl terminé avec succès !')
    #await convertAllFilesToPdfInDir(saveDirectory)
    #await mergeAllPdfInTree(saveDirectory)
    #await mergeGlobalPdf()
    #print('Fusion des PDF par sous-dossier et global terminée !')
    await cleanup()

if __name__ == '__main__':
    asyncio.run(main())
