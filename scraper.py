#!/usr/bin/env python3
"""Coletor Olhos na TV → playlists M3U aninhadas para SS IPTV."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import re
import time
import unicodedata
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
STATE_FILE = ROOT / "channels.json"
PLAYLIST_FILE = ROOT / "playlist.m3u"
LOG = logging.getLogger("olhosnatv")

POST_RE = re.compile(r"/\d{4}/\d{2}/[^/?#]+\.html(?:[?#].*)?$", re.I)
MEDIA_RE = re.compile(
    r"""(?P<url>(?:https?:)?//[^<>"'\s\\]+?\.(?:m3u8|mpd|mp4|m4v|ts|aac|mp3)(?:\?[^<>"'\s\\]*)?)""",
    re.I,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_url(url: str | None, base: str) -> str | None:
    if not url:
        return None
    url = html.unescape(str(url)).strip()
    url = url.replace("\\/", "/").replace("\\u0026", "&").replace("\\x26", "&")
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith(("http://", "https://")):
        url = urljoin(base, url)
    p = urlparse(url)
    if p.scheme not in {"http", "https"} or not p.netloc:
        return None
    return url


def clean_name(text: str) -> str:
    text = html.unescape(str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(assistir|ver|ao vivo|tv online grátis|tv online gratis)\s*[:|\-–—]?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*[-|–—]\s*(Olhos na TV.*)$", "", text, flags=re.I).strip()
    return text or "Canal sem nome"


def title_case_name(name: str) -> str:
    """Capitaliza nomes de canais de forma legível (ex.: 'rede brasil' → 'Rede Brasil')."""
    name = clean_name(name)
    if not name or name == "Canal sem nome":
        return name
    # Já parece ter maiúsculas intencionais (siglas etc.)
    letters = [c for c in name if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.3:
        return name
    small = {"de", "da", "do", "das", "dos", "e", "em", "na", "no", "nas", "nos", "a", "o", "as", "os", "para", "por"}
    parts = []
    for i, w in enumerate(name.split()):
        low = w.casefold()
        if i > 0 and low in small:
            parts.append(low)
        elif w.isupper() and len(w) <= 5:
            parts.append(w)  # mantém siglas curtas em maiúsculas
        else:
            parts.append(w[:1].upper() + w[1:].lower() if w else w)
    return " ".join(parts)


def is_generic_channel_name(name: str) -> bool:
    n = clean_name(name).casefold()
    generics = {
        "canal sem nome", "tv online grátis", "tv online gratis", "canal",
        "ao vivo", "assistir tv", "ver tv", "home", "página inicial",
    }
    return n in generics or len(n) < 2


def name_from_url(url: str) -> str:
    slug = urlparse(url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    slug = re.sub(r"[-_]+", " ", slug)
    return clean_name(slug.replace("%20", " "))


def name_from_logo(url: str) -> str:
    if not url:
        return ""
    path = urlparse(url).path
    filename = path.rsplit("/", 1)[-1]
    filename = re.sub(r"\.(?:webp|png|jpe?g|gif|svg)$", "", filename, flags=re.I)
    filename = re.sub(r"(?:^|[-_ ])(?:logo|tv logo)$", "", filename, flags=re.I)
    return clean_name(filename.replace("%20", " ").replace("_", " ").replace("-", " "))


def best_channel_name(candidates: Iterable[str], page_url: str, logo: str = "") -> str:
    for candidate in candidates:
        value = clean_name(candidate)
        if not is_generic_channel_name(value):
            return title_case_name(value)
    for candidate in (name_from_logo(logo), name_from_url(page_url)):
        if not is_generic_channel_name(candidate):
            return title_case_name(candidate)
    return "Canal sem nome"


def stable_id(source_page: str) -> str:
    return hashlib.sha256(source_page.encode("utf-8")).hexdigest()[:16]


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "sem-categoria"


def ssiptv_attr(value: str) -> str:
    return str(value or "").replace('"', "'").replace("\r", " ").replace("\n", " ").strip()


def raw_base(cfg: dict) -> str:
    return cfg.get("github_raw_base", "").rstrip("/")


def default_state():
    return {
        "version": 2,
        "source": "https://www.olhosnatv.com.br/",
        "updated_at": None,
        "channels": [],
    }


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

class Scraper:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": cfg["user_agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7",
            "Cache-Control": "no-cache",
        })
        self.delay = float(cfg.get("request_delay_seconds", 0.05))
        self.max_workers = int(cfg.get("max_workers", 8))
        self._stream_cache: dict[str, bool] = {}

    def get(self, url: str, timeout: int | None = None, referer: str | None = None):
        last = None
        for attempt in range(1, 4):
            try:
                if self.delay:
                    time.sleep(self.delay)
                headers = {}
                if referer:
                    headers["Referer"] = referer
                r = self.session.get(
                    url,
                    headers=headers,
                    timeout=timeout or self.cfg["request_timeout"],
                    allow_redirects=True,
                )
                r.raise_for_status()
                return r
            except requests.RequestException as exc:
                last = exc
                LOG.warning("GET falhou (%d/3): %s -> %s", attempt, url, exc)
                time.sleep(min(2 * attempt, 5))
        LOG.debug("Último erro: %s", last)
        return None

    # ---- discovery --------------------------------------------------------

    def discover_feed_entries(self) -> OrderedDict[str, dict]:
        """Descobre posts via feed JSON/Atom (com paginação) ou fallback HTML."""
        result: OrderedDict[str, dict] = OrderedDict()
        page_size = int(self.cfg.get("feed_page_size", 150))
        max_pages = int(self.cfg.get("max_feed_pages", 40))
        source = self.cfg["source_url"].rstrip("/") + "/"
        max_channels = int(self.cfg.get("max_channels", 5000))

        # 1) Feed JSON no domínio (preferido — evita XML malformado)
        json_base = f"{source}feeds/posts/default?alt=json&max-results={page_size}"
        if self._paginate_json_feed(json_base, result, max_pages, max_channels):
            LOG.info("Feed JSON: %d entradas totais", len(result))
            return result

        # 2) Feed Atom no domínio
        atom_base = f"{source}feeds/posts/default?alt=atom&max-results={page_size}"
        if self._paginate_atom_feed(atom_base, result, max_pages, max_channels):
            LOG.info("Feed Atom: %d entradas totais", len(result))
            return result

        # 3) Fallback HTML
        LOG.warning("Feed indisponível; usando paginação HTML do blog.")
        return self._discover_html(source, result, max_pages, max_channels)

    def _paginate_json_feed(
        self, first_url: str, result: OrderedDict, max_pages: int, max_channels: int
    ) -> bool:
        url = first_url
        for page in range(max_pages):
            if len(result) >= max_channels:
                break
            LOG.info("Lendo feed JSON página %d: %s", page + 1, url[:100])
            r = self.get(url)
            if not r:
                return bool(result) and page > 0
            body = r.content.lstrip()
            ctype = (r.headers.get("content-type") or "").lower()
            if "json" not in ctype and not body.startswith(b"{"):
                LOG.warning("Resposta não é JSON (ctype=%s)", ctype)
                return False
            try:
                data = r.json()
            except (ValueError, TypeError) as exc:
                LOG.warning("Feed JSON inválido: %s", exc)
                return False

            feed = data.get("feed") or {}
            entries = feed.get("entry") or []
            if isinstance(entries, dict):
                entries = [entries]
            before = len(result)
            for entry in entries:
                link = next(
                    (x.get("href") for x in entry.get("link", [])
                     if x.get("rel") == "alternate" and x.get("href")),
                    None,
                )
                self._add_feed_entry(result, entry, link)
            LOG.info("Entradas acumuladas: %d (+%d)", len(result), len(result) - before)

            if not entries:
                break

            # Próxima página via openSearch / link rel=next / start-index
            next_url = None
            for link in feed.get("link") or []:
                if link.get("rel") == "next" and link.get("href"):
                    next_url = link["href"]
                    # Blogger às vezes devolve blogger.com; reescreve para o domínio
                    if "blogger.com" in next_url:
                        qs = parse_qs(urlparse(next_url).query)
                        start = (qs.get("start-index") or ["1"])[0]
                        maxr = (qs.get("max-results") or [str(self.cfg.get("feed_page_size", 150))])[0]
                        next_url = (
                            f"{self.cfg['source_url'].rstrip('/')}/feeds/posts/default"
                            f"?alt=json&start-index={start}&max-results={maxr}"
                        )
                    break
            if not next_url:
                # Constrói start-index manualmente
                total = int((feed.get("openSearch$totalResults") or {}).get("$t") or 0)
                start = int((feed.get("openSearch$startIndex") or {}).get("$t") or 1)
                per = int((feed.get("openSearch$itemsPerPage") or {}).get("$t") or len(entries))
                if start + per <= total:
                    next_url = self._with_start_index(first_url, start + per)
            if not next_url:
                break
            url = next_url
        return bool(result)

    def _paginate_atom_feed(
        self, first_url: str, result: OrderedDict, max_pages: int, max_channels: int
    ) -> bool:
        url = first_url
        for page in range(max_pages):
            if len(result) >= max_channels:
                break
            LOG.info("Lendo feed Atom página %d", page + 1)
            r = self.get(url)
            if not r:
                return bool(result) and page > 0
            body = r.content.lstrip()
            ctype = (r.headers.get("content-type") or "").lower()
            if "xml" not in ctype and not body.startswith((b"<?xml", b"<feed")):
                return False
            try:
                soup = BeautifulSoup(r.content, "xml")
            except Exception as exc:
                LOG.warning("Feed XML inválido: %s", exc)
                return False
            entries = soup.find_all("entry")
            before = len(result)
            for entry in entries:
                link_tag = entry.find("link", attrs={"rel": "alternate"}) or entry.find("link")
                link = link_tag.get("href") if link_tag else None
                categories = [t.get("term", "") for t in entry.find_all("category")]
                self._add_feed_entry(
                    result,
                    {
                        "title": {"$t": entry.find("title").get_text(" ", strip=True) if entry.find("title") else "Canal"},
                        "link": [{"rel": "alternate", "href": link}] if link else [],
                        "category": [{"term": x} for x in categories],
                        "content": {"$t": entry.find("content").get_text() if entry.find("content") else ""},
                        "summary": {"$t": entry.find("summary").get_text() if entry.find("summary") else ""},
                    },
                    link,
                )
            LOG.info("Entradas acumuladas: %d (+%d)", len(result), len(result) - before)
            if not entries:
                break
            next_link = soup.find("link", attrs={"rel": "next"})
            if not next_link or not next_link.get("href"):
                break
            url = next_link["href"]
            if "blogger.com" in url:
                qs = parse_qs(urlparse(url).query)
                start = (qs.get("start-index") or ["1"])[0]
                maxr = (qs.get("max-results") or [str(self.cfg.get("feed_page_size", 150))])[0]
                url = (
                    f"{self.cfg['source_url'].rstrip('/')}/feeds/posts/default"
                    f"?alt=atom&start-index={start}&max-results={maxr}"
                )
        return bool(result)

    @staticmethod
    def _with_start_index(url: str, start: int) -> str:
        p = urlparse(url)
        qs = parse_qs(p.query)
        qs["start-index"] = [str(start)]
        return urlunparse(p._replace(query=urlencode({k: v[0] for k, v in qs.items()})))

    def _discover_html(
        self, source: str, result: OrderedDict, max_pages: int, max_channels: int
    ) -> OrderedDict:
        next_url = source
        visited: set[str] = set()
        for page in range(max_pages):
            if len(result) >= max_channels:
                break
            if not next_url or next_url in visited:
                break
            visited.add(next_url)
            LOG.info("Lendo página HTML do blog: %d", page + 1)
            r = self.get(next_url)
            if not r:
                if page == 0:
                    raise RuntimeError("Não foi possível acessar o site Olhos na TV.")
                break
            soup = BeautifulSoup(r.text, "html.parser")
            found_before = len(result)
            for a in soup.find_all("a", href=True):
                href = normalize_url(a.get("href"), next_url)
                if not href or not POST_RE.search(urlparse(href).path):
                    continue
                title = clean_name(a.get_text(" ", strip=True))
                if is_generic_channel_name(title):
                    title = name_from_url(href)
                categories: list[str] = []
                parent = a
                for _ in range(4):
                    parent = getattr(parent, "parent", None)
                    if not parent:
                        break
                    for ca in parent.find_all("a", href=True):
                        txt = clean_name(ca.get_text(" ", strip=True))
                        chref = ca.get("href", "")
                        if txt and "/search/label/" in chref:
                            categories.append(txt)
                key = href.split("#", 1)[0]
                if key not in result:
                    result[key] = {
                        "name": title_case_name(title),
                        "categories": sorted(set(categories), key=str.casefold),
                        "logo": "",
                    }
            LOG.info(
                "Posts encontrados nesta página: +%d (total %d)",
                len(result) - found_before,
                len(result),
            )
            candidates = []
            for a in soup.find_all("a", href=True):
                txt = clean_name(a.get_text(" ", strip=True)).casefold()
                href = normalize_url(a.get("href"), next_url)
                if not href:
                    continue
                if (
                    "older posts" in txt
                    or "postagens mais antigas" in txt
                    or "mais antigas" in txt
                    or txt == "older"
                ):
                    candidates.append(href)
                elif "updated-max=" in href and "max-results=" in href:
                    candidates.append(href)
            next_url = next((u for u in candidates if u not in visited), None)
            if not next_url:
                break

        if not result:
            raise RuntimeError("O site foi acessado, mas nenhuma postagem de canal foi encontrada.")
        return result

    def _add_feed_entry(self, result: OrderedDict[str, dict], entry: dict, link: str | None) -> None:
        link = normalize_url(link, self.cfg["source_url"])
        if not link or not POST_RE.search(urlparse(link).path):
            return
        key = link.split("#", 1)[0]
        if key in result:
            return

        def val(obj, key_name, default=""):
            x = obj.get(key_name, default) if isinstance(obj, dict) else default
            if isinstance(x, dict):
                return x.get("$t", default)
            return x

        title = clean_name(val(entry, "title", "Canal"))
        tags = entry.get("tags") or entry.get("category") or []
        categories = []
        for tag in tags:
            term = tag.get("term", "") if isinstance(tag, dict) else ""
            term = clean_name(term)
            if term and term.upper() not in {"BLOG", "UNCATEGORIZED"}:
                categories.append(term)
        content = val(entry, "content", "") or val(entry, "summary", "")
        soup = BeautifulSoup(content, "html.parser")
        img = soup.find("img")
        logo = normalize_url(img.get("src") or img.get("data-src"), link) if img else ""
        if is_generic_channel_name(title):
            title = name_from_url(link)
        result[key] = {
            "name": title_case_name(title),
            "categories": sorted(set(categories), key=str.casefold),
            "logo": logo or "",
        }

    # ---- media extraction -------------------------------------------------

    def extract_media_urls(self, text: str, base: str) -> list[str]:
        text = html.unescape(text or "")
        text = text.replace("\\/", "/").replace("\\u0026", "&").replace("\\x26", "&")
        found: list[str] = []
        for m in MEDIA_RE.finditer(text):
            u = normalize_url(m.group("url"), base)
            if u:
                found.append(u)
        patterns = [
            r"""(?:file|source|src|url|streamUrl|stream_url|hls|dash)\s*[:=]\s*["']([^"']+)["']""",
            r"""(?:file|source|src|url|streamUrl|stream_url|hls|dash)\s*[:=]\s*([^,}\s"']+)""",
            r"""["'](?:file|source|src|url|hls|dash)["']\s*:\s*["']([^"']+)["']""",
        ]
        for pattern in patterns:
            for m in re.finditer(pattern, text, re.I):
                u = normalize_url(m.group(1), base)
                if u:
                    found.append(u)
        return list(dict.fromkeys(found))

    def extract_jmv_stream(self, text: str) -> list[str]:
        text = html.unescape(text or "").replace("\\/", "/")
        ids = re.findall(r"""["'](LVW-\d+)["']""", text, re.I)
        if not ids:
            return []
        tokens = re.findall(r"""["']([A-Za-z0-9_-]{10,80})["']""", text)
        out = []
        for channel_id in ids:
            for token in tokens:
                if token == channel_id:
                    continue
                if token.lower().startswith(("http", "www", "cdn")):
                    continue
                out.append(f"https://cdn.live.br1.jmvstream.com/w/{channel_id}/{token}/playlist.m3u8")
        return list(dict.fromkeys(out[:10]))

    def looks_like_media(self, url: str) -> bool:
        path = urlparse(url).path.lower()
        return any(path.endswith(ext) for ext in self.cfg["allowed_media_extensions"])

    def validate_stream(self, url: str, referer: str | None = None) -> bool:
        headers = {"User-Agent": self.cfg["user_agent"], "Range": "bytes=0-4095"}
        if referer:
            headers["Referer"] = referer
        try:
            r = self.session.get(
                url,
                headers=headers,
                timeout=self.cfg["media_timeout"],
                stream=True,
                allow_redirects=True,
            )
            ok = r.status_code < 400
            r.close()
            if ok:
                return True
            if r.status_code in {401, 403, 405}:
                r = self.session.head(
                    url,
                    headers=headers,
                    timeout=self.cfg["media_timeout"],
                    allow_redirects=True,
                )
                ok = r.status_code < 400
                r.close()
            return ok
        except requests.RequestException:
            return False

    def resolve_candidates(self, candidates: Iterable[str], base: str) -> list[str]:
        max_depth = int(self.cfg.get("max_resolve_depth", 2))
        max_candidates = int(self.cfg.get("max_candidates_per_channel", 6))
        queue = [(u, 0, base) for u in list(dict.fromkeys(candidates))[:max_candidates] if u]
        seen: set[str] = set()
        final: list[str] = []

        while queue and len(seen) < max_candidates * 3:
            url, depth, parent = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            if url in self._stream_cache:
                if self._stream_cache[url]:
                    return [url]
                continue

            if self.looks_like_media(url):
                ok = (not self.cfg.get("validate_streams", True) or self.validate_stream(url, parent))
                self._stream_cache[url] = ok
                if ok:
                    final.append(url)
                    if not self.cfg.get("keep_multiple_streams_per_channel", False):
                        return final
                continue

            if depth >= max_depth:
                continue
            r = self.get(url, timeout=self.cfg["media_timeout"], referer=parent)
            if not r:
                continue
            ctype = r.headers.get("content-type", "").lower()
            if "html" not in ctype and "text" not in ctype and "javascript" not in ctype:
                continue
            text = r.text
            children: list[str] = []
            children.extend(self.extract_media_urls(text, r.url))
            children.extend(self.extract_jmv_stream(text))
            soup = BeautifulSoup(text, "html.parser")
            for tag in soup.find_all(["iframe", "video", "source"]):
                for attr in ("src", "data-src", "data-url", "href"):
                    child = normalize_url(tag.get(attr), r.url)
                    if child:
                        children.append(child)
            for child in list(dict.fromkeys(children))[:max_candidates]:
                queue.append((child, depth + 1, r.url))
        return final

    def extract_channel(self, page_url: str, meta: dict, old: dict | None):
        r = self.get(page_url)
        if not r:
            if old and old.get("stream_url") and self.validate_stream(old["stream_url"], page_url):
                old = dict(old)
                old["active"] = True
                old["last_seen"] = datetime.now(timezone.utc).isoformat()
                return old
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        logo = meta.get("logo", "")
        if not logo:
            img = soup.select_one("article img, .post img, img")
            if img:
                logo = normalize_url(img.get("src") or img.get("data-src"), page_url) or ""

        title_candidates: list[str] = []
        title_node = soup.select_one("h1.post-title, h1.entry-title, h1")
        if title_node:
            title_candidates.append(title_node.get_text(" ", strip=True))
        og = soup.select_one('meta[property="og:title"], meta[name="twitter:title"]')
        if og and og.get("content"):
            title_candidates.append(og.get("content"))
        for img in soup.select("article img[alt], .post img[alt], img[alt]")[:3]:
            if img.get("alt"):
                title_candidates.append(img.get("alt"))
        title_candidates.append(meta.get("name", ""))
        name = best_channel_name(title_candidates, page_url, logo)

        candidates = self.extract_media_urls(r.text, page_url)
        candidates += self.extract_jmv_stream(r.text)
        for tag in soup.find_all(["iframe", "video", "source"]):
            for attr in ("src", "data-src", "data-url", "href"):
                u = normalize_url(tag.get(attr), page_url)
                if u:
                    candidates.append(u)

        media = self.resolve_candidates(list(dict.fromkeys(candidates)), page_url)
        if not media and old and old.get("stream_url"):
            if self.validate_stream(old["stream_url"], page_url):
                media = [old["stream_url"]]

        if not media:
            return None

        return {
            "id": stable_id(page_url),
            "name": name,
            "categories": meta.get("categories") or ["Sem categoria"],
            "logo": logo,
            "source_page": page_url,
            "stream_url": media[0],
            "active": True,
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# M3U rendering (SS IPTV nested playlists)
# ---------------------------------------------------------------------------

def render_channel_lines(channel: dict, group: str) -> list[str]:
    safe_name = ssiptv_attr(channel["name"])
    safe_group = ssiptv_attr(group)
    logo = ssiptv_attr(channel.get("logo", ""))
    attrs = (
        f'tvg-id="{ssiptv_attr(channel["id"])}" '
        f'tvg-name="{safe_name}" '
        f'group-title="{safe_group}"'
    )
    if logo:
        attrs += f' tvg-logo="{logo}"'
    return [f"#EXTINF:-1 {attrs},{safe_name}", channel["stream_url"]]


def render_category_playlists(state: dict, cfg: dict) -> dict[str, str]:
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for channel in state.get("channels", []):
        for group in channel.get("categories") or ["Sem categoria"]:
            groups.setdefault(group, []).append(channel)

    result: dict[str, str] = {}
    used: set[str] = set()
    for group in sorted(groups, key=str.casefold):
        base = f"categoria-{slugify(group)}"
        filename = base + ".m3u"
        n = 2
        while filename in used:
            filename = f"{base}-{n}.m3u"
            n += 1
        used.add(filename)

        lines = ['#EXTM3U size="medium"']
        for channel in sorted(groups[group], key=lambda c: c["name"].casefold()):
            lines.extend(render_channel_lines(channel, group))
        result[filename] = "\n".join(lines) + "\n"
    return result


def _category_label_from_filename(filename: str, state: dict) -> str:
    """Recupera o nome original da categoria a partir do slug do ficheiro."""
    slug = filename[len("categoria-"):-4]
    # Remove sufixo numérico de colisão (ex.: tvs-abertas-2)
    base_slug = re.sub(r"-\d+$", "", slug)
    for channel in state.get("channels", []):
        for group in channel.get("categories") or []:
            if slugify(group) == base_slug or slugify(group) == slug:
                return group
    return slug.replace("-", " ").strip().title()


def render_root_m3u(state: dict, cfg: dict, category_files: dict[str, str]) -> str:
    base = raw_base(cfg)
    lines = ['#EXTM3U size="medium"']
    for filename in sorted(category_files, key=str.casefold):
        label = _category_label_from_filename(filename, state)
        url = f"{base}/{filename}" if base else filename
        lines.append(f'#EXTINF:0 type="playlist" tvg-name="{ssiptv_attr(label)}",{ssiptv_attr(label)}')
        lines.append("#EXTSIZE:medium")
        lines.append(url)
    return "\n".join(lines) + "\n"


def cleanup_old_category_files(active_files: set[str]) -> None:
    for path in ROOT.glob("categoria-*.m3u"):
        if path.name not in active_files:
            path.unlink()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Coleta canais Olhos na TV e gera M3U para SS IPTV")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Não grava ficheiros")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cfg = load_json(CONFIG_FILE, {})
    if not cfg:
        raise SystemExit("config.json ausente ou inválido")

    scraper = Scraper(cfg)
    old_state = load_json(STATE_FILE, default_state())
    old_by_url = {c.get("source_page"): c for c in old_state.get("channels", []) if c.get("source_page")}

    LOG.info("Lendo feed Blogger do próprio domínio")
    entries = scraper.discover_feed_entries()
    if not entries:
        raise RuntimeError("Nenhum canal descoberto; a playlist anterior não será alterada.")

    discovered: list[dict] = []
    failures = 0
    total = len(entries)
    LOG.info("Processando %d páginas com até %d workers", total, scraper.max_workers)

    def work(item):
        page_url, meta = item
        return page_url, meta, scraper.extract_channel(page_url, meta, old_by_url.get(page_url))

    with ThreadPoolExecutor(max_workers=scraper.max_workers) as pool:
        futures = [pool.submit(work, item) for item in entries.items()]
        for i, future in enumerate(as_completed(futures), 1):
            page_url, meta, channel = future.result()
            LOG.info("[%d/%d] %s -> %s", i, total, meta["name"], "OK" if channel else "falhou")
            if channel:
                discovered.append(channel)
            else:
                failures += 1

    if not discovered:
        raise RuntimeError(
            f"0 canais ativos encontrados entre {total} posts. "
            "A playlist anterior foi preservada."
        )

    max_failure_ratio = float(cfg.get("max_failure_ratio", 0.90))
    if total >= 10 and failures / total > max_failure_ratio:
        raise RuntimeError(
            f"{failures}/{total} canais falharam ({failures / total:.0%}). "
            "Possível bloqueio/falha da fonte; playlist anterior preservada."
        )

    by_id = {c["id"]: c for c in discovered}
    channels = sorted(
        by_id.values(),
        key=lambda c: ((c.get("categories") or ["ZZZ"])[0].casefold(), c["name"].casefold()),
    )

    new_state = {
        "version": 2,
        "source": cfg["source_url"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "channels": channels,
    }

    if args.dry_run:
        LOG.info("Dry-run: %d canais ativos, nada gravado.", len(channels))
        return 0

    category_files = render_category_playlists(new_state, cfg)
    for filename, content in category_files.items():
        (ROOT / filename).write_text(content, encoding="utf-8", newline="\n")
    cleanup_old_category_files(set(category_files))

    save_json(STATE_FILE, new_state)
    PLAYLIST_FILE.write_text(
        render_root_m3u(new_state, cfg, category_files), encoding="utf-8", newline="\n"
    )

    old_ids = {c.get("id") for c in old_state.get("channels", [])}
    new_ids = {c.get("id") for c in channels}
    LOG.info(
        "RESULTADO: descobertos=%d ativos=%d falhas=%d novos=%d removidos=%d categorias=%d",
        total,
        len(channels),
        failures,
        len(new_ids - old_ids),
        len(old_ids - new_ids),
        len(category_files),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
