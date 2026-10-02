"""Helpers: link parsing, per-deal snippets, Amazon affiliate conversion, scraping, formatting."""
import asyncio
import html
import io
import ipaddress
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import aiohttp
from telethon import utils as tl_utils
from telethon.helpers import add_surrogate, del_surrogate
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl, PeerChannel, PeerChat

log = logging.getLogger("utils")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SHORT_HOSTS = {"amzn.to", "amzn.in", "amzn.eu", "amzn.asia", "a.co", "link.amazon", "amzn-to.co", "amzlink.to", "amazn.lt"}
SHORT_HOSTS |= {h.strip().lower() for h in os.getenv("EXTRA_SHORT_HOSTS", "").split(",") if h.strip()}
# short links written without a scheme, e.g. "link.amazon/B07o4k6Hr"
SHORT_BARE_RE = re.compile(
    r"(?<![\w@./-])((?:" + "|".join(re.escape(h) for h in sorted(SHORT_HOSTS)) + r")/[^\s<>\"']+)", re.I
)
AMAZON_HOST_RE = re.compile(
    r"^(?:www\.|m\.|smile\.)?amazon\.(?:com|[a-z]{2}|co\.[a-z]{2}|com\.[a-z]{2})$"
)
ASIN_RE = re.compile(
    r"/(?:dp|gp/product|gp/aw/d|gp/offer-listing|gp/aw/ol|product|product-reviews|exec/obidos/ASIN)/([A-Za-z0-9]{10})(?:[/?#]|$)"
)
# Links to these never lead to Amazon -> not worth a redirect lookup. Every OTHER host is resolved,
# so any third-party shortener (bit.ly, cutt.ly, amazn.lt, ...) that lands on Amazon is supported.
NON_AMAZON_DOMAINS = {
    "t.me", "telegram.me", "telegram.org", "telegra.ph", "youtube.com", "youtu.be", "instagram.com",
    "facebook.com", "fb.com", "fb.me", "twitter.com", "x.com", "whatsapp.com", "wa.me", "linkedin.com",
    "flipkart.com", "fkrt.it", "fkrt.cc", "fkrt.site", "myntra.com", "ajio.com", "meesho.com",
    "snapdeal.com", "nykaa.com", "jiomart.com", "tatacliq.com", "croma.com", "reliancedigital.in",
    "paytm.com", "google.com", "goo.gl", "play.google.com", "apple.com", "wikipedia.org",
}
MAX_RESOLVE_PER_POST = 12
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
# amazon.in paths that are redirectors (no ASIN in the URL itself) -> must be followed
NEEDS_FOLLOW_RE = re.compile(r"^/(?:d/|l/|gp/r\.html|gp/redirect\.html|gp/slredirect/|gp/f\.html)", re.I)
DROP_PARAMS = {"tag", "linkcode", "linkid", "ascsubtag", "creative", "creativeasin", "camp", "th", "psc", "_encoding"}

ID_RE = re.compile(r"^-?\d{5,20}$")
USERNAME_RE = re.compile(r"^(?:@|(?:https?://)?t\.me/)([A-Za-z][A-Za-z0-9_]{3,31})/?$")

MAX_LINES = 4
MAX_CHARS = 350
TRIM = " \t:-–—|•·>»*_~`#=\u00a0\ufe0f➡👉🔗🛒"
CTA_RE = re.compile(
    r"^(?:buy|link|links|grab|order|get|click|check|deal|offer|price|shop|visit|available)"
    r"(?:\s+(?:now|here|link|it|price|on amazon))?$",
    re.I,
)


# ---------------------------------------------------------------- ID helpers
def peers_for(raw: str) -> list:
    """Possible Telegram peers for a raw numeric ID (marked -100xxx, -xxx or bare)."""
    n = int(raw)
    if n < 0:
        real_id, cls = tl_utils.resolve_id(n)
        return [cls(real_id)]
    return [PeerChannel(n), PeerChat(n)]


def marked_ids(raw: str) -> list:
    return [tl_utils.get_peer_id(p) for p in peers_for(raw)]


# ------------------------------------------------------------- link parsing
@dataclass
class Link:
    url: str
    start: int
    end: int
    label: str = ""


@dataclass
class Deal:
    title: str
    url: str          # affiliate URL
    asin: Optional[str]
    page: str         # clean page URL used for scraping


def _norm_url(u: str) -> str:
    u = del_surrogate(u).strip().rstrip(".,;)]}>'\"")
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u


def parse_links(message):
    """Return (text in UTF-16 'surrogate' space, links sorted by position)."""
    s = add_surrogate(message.raw_text or "")
    links: list[Link] = []
    for ent in message.entities or []:
        a, b = ent.offset, ent.offset + ent.length
        if isinstance(ent, MessageEntityTextUrl):
            links.append(Link(_norm_url(ent.url), a, b, del_surrogate(s[a:b])))
        elif isinstance(ent, MessageEntityUrl):
            links.append(Link(_norm_url(s[a:b]), a, b))
    for m in URL_RE.finditer(s):  # fallback for URLs without entities
        if not any(l.start < m.end() and m.start() < l.end for l in links):
            links.append(Link(_norm_url(m.group(0)), m.start(), m.end()))
    for m in SHORT_BARE_RE.finditer(s):
        if not any(l.start < m.end() and m.start() < l.end for l in links):
            links.append(Link(_norm_url(m.group(1)), m.start(1), m.end(1)))
    links.sort(key=lambda l: l.start)
    return s, links


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _safe_host(host: str) -> bool:
    """Never fetch internal/private targets (SSRF guard)."""
    if not host or host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)
    except ValueError:
        return True


def should_try(url: str) -> bool:
    """Any http(s) link might be an Amazon shortener, so try everything except known non-Amazon sites."""
    h = host_of(url)
    if not h or not _safe_host(h):
        return False
    if h in SHORT_HOSTS or AMAZON_HOST_RE.match(h):
        return True
    return not any(h == d or h.endswith("." + d) for d in NON_AMAZON_DOMAINS)


# ----------------------------------------------------------- per-deal context
def _clean(text: str, tail: bool) -> str:
    text = URL_RE.sub(" ", del_surrogate(text))
    lines = []
    for ln in text.splitlines():
        ln = ln.strip().rstrip(TRIM).strip()
        if not re.search(r"[^\W_]", ln):  # no letters/digits -> emoji/punctuation only
            continue
        if CTA_RE.match(ln):
            continue
        lines.append(ln)
    lines = lines[-MAX_LINES:] if tail else lines[:MAX_LINES]
    out = "\n".join(lines)
    if len(out) > MAX_CHARS:
        out = out[:MAX_CHARS].rsplit(" ", 1)[0].rstrip() + "…"
    return out


def snippet_for(s: str, links: list, i: int) -> str:
    """Caption text that belongs to link i: text before it (back to the previous link),
    else text after it (up to the next link). A single-link post uses the whole text."""
    if len(links) == 1:
        l = links[0]
        return _clean(s[: l.start] + "\n" + s[l.end:], tail=False)
    prev_end = links[i - 1].end if i > 0 else 0
    next_start = links[i + 1].start if i + 1 < len(links) else len(s)
    out = _clean(s[prev_end: links[i].start], tail=True)
    if not out:
        out = _clean(s[links[i].end: next_start], tail=False)
    if not out and len(links[i].label.split()) >= 2:
        out = _clean(links[i].label, tail=False)
    return out


# ------------------------------------------------------- affiliate conversion
_REDIRECT_PATS = (
    r'http-equiv=["\']?refresh["\']?[^>]*?content=["\'][^"\']*?url=([^"\'>\s]+)',
    r'location\.replace\(\s*["\']([^"\']+)',
    r'(?:window\.|document\.)?location(?:\.href)?\s*=\s*["\']([^"\']+)',
    r'(https?://(?:www\.|m\.|smile\.)?amazon\.[a-z.]{2,6}/[^\s"\'<>\\]+)',
)


def _find_redirect(body: str, base: str, allow_scan: bool = True) -> Optional[str]:
    """Some shorteners answer 200 with an HTML/JS redirect instead of a 30x."""
    # last pattern = "any Amazon URL in the page": only safe on Amazon-ish shortener hosts
    for pat in (_REDIRECT_PATS if allow_scan else _REDIRECT_PATS[:-1]):
        m = re.search(pat, body, re.I)
        if m:
            return urljoin(base, html.unescape(m.group(1)).replace("\\/", "/"))
    return None


async def _resolve_uncached(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    """Follow redirects (30x, meta-refresh or JS) until we land on a real Amazon page URL,
    without downloading the Amazon page itself. Works for any shortener."""
    cur, status = url, None
    for _ in range(8):
        host = host_of(cur)
        if not _safe_host(host):  # SSRF guard on every hop
            break
        if AMAZON_HOST_RE.match(host) and not NEEDS_FOLLOW_RE.match(urlsplit(cur).path):
            return cur
        try:
            async with session.get(
                cur, headers=HEADERS, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                status = r.status
                loc = r.headers.get("Location")
                if status in (301, 302, 303, 307, 308) and loc:
                    cur = urljoin(cur, loc)
                    continue
                if status == 200:
                    body = (await r.content.read(300_000)).decode("utf-8", "ignore")
                    # "any Amazon URL in the page" scan only on Amazon-ish shortener hosts
                    nxt = _find_redirect(
                        body, cur, allow_scan=host in SHORT_HOSTS or "amz" in host or "amazon" in host
                    )
                    if nxt and nxt != cur:
                        cur = nxt
                        continue
        except Exception as e:
            log.warning("resolve failed for %s: %s", url, e)
            return None
        break
    (log.warning if host_of(url) in SHORT_HOSTS else log.info)(
        "not an Amazon link / unresolved: %s (last status %s, last url %s)", url, status, cur
    )
    return None


_RESOLVE_CACHE: dict = {}  # url -> (final|None, expires_at)


async def resolve_final(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    now = time.monotonic()
    hit = _RESOLVE_CACHE.get(url)
    if hit and hit[1] > now:
        return hit[0]
    final = await _resolve_uncached(session, url)
    if len(_RESOLVE_CACHE) > 1000:
        _RESOLVE_CACHE.pop(next(iter(_RESOLVE_CACHE)))
    _RESOLVE_CACHE[url] = (final, now + (6 * 3600 if final else 300))  # failures retried after 5 min
    return final


def build_affiliate(final: str, tag: str):
    """-> (asin|None, affiliate_url, clean_page_url)"""
    p = urlsplit(final)
    host = "www." + re.sub(r"^(www|m|smile)\.", "", (p.hostname or "").lower())
    m = ASIN_RE.search(p.path)
    qasin = next((v for k, v in parse_qsl(p.query) if k.lower() in ("asin", "pd_rd_i") and re.fullmatch(r"[A-Za-z0-9]{10}", v)), None)
    if m or qasin:
        asin = (m.group(1) if m else qasin).upper()
        return asin, f"https://{host}/dp/{asin}?tag={tag}", f"https://{host}/dp/{asin}"
    q = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in DROP_PARAMS and not k.lower().startswith(("pf_rd", "pd_rd", "ref"))
    ]
    path = re.sub(r"/ref=[^/?#]*", "", p.path)
    page = urlunsplit(("https", host, path, urlencode(q), ""))
    q.append(("tag", tag))
    return None, urlunsplit(("https", host, path, urlencode(q), "")), page


async def build_deals(session, s: str, links: list, tag: str) -> list:
    cand = [i for i, l in enumerate(links) if should_try(l.url)][:MAX_RESOLVE_PER_POST]
    if not cand:
        return []
    finals = await asyncio.gather(*(resolve_final(session, links[i].url) for i in cand))
    deals, seen = [], set()
    for i, final in zip(cand, finals):
        if not final:
            continue
        asin, aff, page = build_affiliate(final, tag)
        key = asin or aff
        if key in seen:
            continue
        seen.add(key)
        deals.append(Deal(title=snippet_for(s, links, i), url=aff, asin=asin, page=page))
    return deals


# ------------------------------------------------------------------ scraping
def _meta_content(body: str, key: str) -> Optional[str]:
    pats = (
        rf'<meta[^>]+?(?:property|name)=["\']{key}["\'][^>]*?content=(["\'])(.*?)\1',
        rf'<meta[^>]+?content=(["\'])(.*?)\1[^>]*?(?:property|name)=["\']{key}["\']',
    )
    for pat in pats:
        m = re.search(pat, body, re.I)
        if m and m.group(2).strip():
            return html.unescape(m.group(2)).strip()
    return None


def _extract_title(body: str) -> Optional[str]:
    t = _meta_content(body, "og:title")
    if not t:
        m = re.search(r'id=["\']productTitle["\'][^>]*>\s*(.*?)\s*</span>', body, re.S | re.I)
        t = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else None
    if not t:
        m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
        t = html.unescape(m.group(1)).strip() if m else None
    if not t:
        return None
    t = re.split(r"\s*:\s*Amazon\.", t)[0]
    t = re.sub(r"^Amazon\.[a-z.]+\s*:\s*", "", t, flags=re.I).strip()
    if re.search(r"robot check|captcha|something went wrong|page not found|^amazon", t, re.I):
        return None
    return t[:MAX_CHARS] or None


def _extract_image(body: str) -> Optional[str]:
    for key in ("og:image", "twitter:image"):
        u = _meta_content(body, key)
        if u and u.startswith("http"):
            return u
    tag = re.search(r'<img[^>]+id=["\']landingImage["\'][^>]*>', body, re.I)
    if tag:
        t = tag.group(0)
        m = re.search(r'data-old-hires=["\'](https?://[^"\']+)', t)
        if m:
            return html.unescape(m.group(1))
        m = re.search(r'data-a-dynamic-image=(["\'])(.*?)\1', t, re.S)
        if m:
            m2 = re.search(r"https?://[^\"\\\s]+", html.unescape(m.group(2)))
            if m2:
                return m2.group(0)
    for pat in (r'"hiRes"\s*:\s*"(https?:[^"]+)"', r'"large"\s*:\s*"(https?:[^"]+)"'):
        m = re.search(pat, body)
        if m:
            return m.group(1).replace("\\/", "/")
    return None


async def fetch_product_meta(session, page_url: str, asin: Optional[str]) -> dict:
    meta = {"title": None, "image": None}
    try:
        async with session.get(
            page_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)
        ) as r:
            if r.status == 200:
                body = await r.text(errors="ignore")
                meta["title"] = _extract_title(body)
                meta["image"] = _extract_image(body)
    except Exception as e:
        log.warning("meta fetch failed for %s: %s", page_url, e)
    if not meta["image"] and asin:  # best-effort legacy image endpoint
        meta["image"] = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01.LZZZZZZZ.jpg"
    return meta


async def download_image(session, url: Optional[str]) -> Optional[io.BytesIO]:
    if not url:
        return None
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as r:
            ctype = r.content_type or ""
            if r.status != 200 or not ctype.startswith("image/"):
                return None
            data = await r.read()
        if not (2000 < len(data) < 10_000_000):  # tiny = Amazon's 1x1 placeholder
            return None
        bio = io.BytesIO(data)
        bio.name = "deal." + ("png" if "png" in ctype else "webp" if "webp" in ctype else "jpg")
        return bio
    except Exception as e:
        log.warning("image download failed for %s: %s", url, e)
        return None


# ---------------------------------------------------------------- formatting
def format_post(title: str, url: str, header: Optional[str], footer: Optional[str], limit: int = 1000,
                link_line: bool = True) -> str:
    """HTML post: [header] / 🛍️ bold title / [footer] / 👉 Check Price link."""

    def build(t: str) -> str:
        parts = []
        if header:
            parts.append(html.escape(header, quote=False))
        parts.append(f"🛍️ <b>{html.escape(t, quote=False)}</b>")
        if footer:
            parts.append(html.escape(footer, quote=False))
        if link_line:
            parts.append(f'👉 <a href="{html.escape(url, quote=True)}">Check Price</a>')
        return "\n\n".join(parts)

    text = build(title)
    while len(text) > limit and len(title) > 40:
        title = title[: int(len(title) * 0.8)].rstrip() + "…"
        text = build(title)
    return text
