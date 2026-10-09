import re
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

PROXIES = {
    "http": "socks5h://127.0.0.1:9050",
    "https": "socks5h://127.0.0.1:9050",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/115.0"
}

TIMEOUT_SECONDS = 30

PATTERNS = {
    "bitcoin_wallets": re.compile(r"\b(?:[13][a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[a-zA-HJ-NP-Z0-9]{39,59})\b"),
    "ethereum_wallets": re.compile(r"\b0x[a-fA-F0-9]{40}\b"),
    "monero_wallets": re.compile(r"\b[48][0-9ABa-zA-Z]{94}\b"),
    "emails": re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    "phone_numbers": re.compile(r"\b(?:\+?\d{1,3}[-.●\s]?)?\(?\d{3}\)?[-.●\s]?\d{3}[-.●\s]?\d{4}\b"),
    "pgp_keys": re.compile(r"-----BEGIN PGP PUBLIC KEY BLOCK-----[\s\S]*?-----END PGP PUBLIC KEY BLOCK-----"),
    "onion_links": re.compile(r"\b[a-z2-7]{56}\.onion\b", re.IGNORECASE),
    "ipv4_addresses": re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b"),
    "handles": re.compile(r"(?:Author|User|Username|Profile|Member):\s*([a-zA-Z0-9_-]{3,20})|@([a-zA-Z0-9_-]{3,20})", re.IGNORECASE),
}

def extract_entities(text: str) -> dict:
    return {
        key: sorted(set(pattern.findall(text)))
        for key, pattern in PATTERNS.items()
    }

def build_record(source: str, text: str, headers: dict = None) -> dict:
    entities = extract_entities(text)
    if "handles" in entities:
        flat_handles = []
        for match in entities["handles"]:
            flat_handles.extend([m for m in match if m])
        entities["handles"] = sorted(set(flat_handles))

    total_identifiers = sum(len(v) for v in entities.values())
    return {
        "source": source,
        "last_scan_date": datetime.now(timezone.utc).isoformat(),
        "category": "unclassified",
        "attribution_confidence": None,
        "identifiers": entities,
        "identifier_count": total_identifiers,
        "infrastructure": {
            "server_headers": headers or {}
        }
    }

def html_to_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    for script_or_style in soup(["script", "style", "noscript"]):
        script_or_style.decompose()
    return soup.get_text(separator=" ", strip=True)

def extract_links(base_url: str, raw_html: str, same_host_only: bool) -> list:
    soup = BeautifulSoup(raw_html, "html.parser")
    base_host = urlparse(base_url).netloc
    found = []
    for tag in soup.find_all("a", href=True):
        absolute = urljoin(base_url, tag["href"])
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if same_host_only and parsed.netloc != base_host:
            continue
        found.append(absolute)
    return found

def fetch_url(url: str, max_retries: int = 2) -> tuple:
    print(f"[SCRAPER] Connecting to {url} via Tor/ClearNet...")
    for attempt in range(1, max_retries + 1):
        try:
            # Note: We use PROXIES. If Tor is not running, this will fail. 
            # In a robust setup, we might detect .onion to use proxies, or just fallback.
            # We'll use proxies if it's an onion, else try without if proxy fails.
            use_proxies = PROXIES if ".onion" in url else None
            
            try:
                response = requests.get(url, proxies=use_proxies, headers=HEADERS, timeout=TIMEOUT_SECONDS)
            except Exception:
                # Fallback to no proxy if it fails (useful for local dev testing with clearnet)
                response = requests.get(url, headers=HEADERS, timeout=TIMEOUT_SECONDS)

            if response.status_code == 200:
                important_headers = {}
                for key in ["Server", "X-Powered-By", "Via"]:
                    if key in response.headers:
                        important_headers[key] = response.headers[key]
                return html_to_text(response.text), response.text, important_headers
        except requests.exceptions.RequestException as e:
            pass
        time.sleep(1)
    return None, None, None

def run_url_mode(start_url: str, max_depth: int = 0, max_pages: int = 1) -> list:
    visited = set()
    queue = deque([(start_url, 0)])
    records = []

    while queue and len(visited) < max_pages:
        url, depth = queue.popleft()
        if url in visited:
            continue
        visited.add(url)

        text, raw_html, headers = fetch_url(url)
        if text is None:
            continue

        record = build_record(url, text, headers)
        record["crawl_depth"] = depth
        records.append(record)

        if depth < max_depth and len(visited) < max_pages:
            for link in extract_links(url, raw_html, True):
                if link not in visited:
                    queue.append((link, depth + 1))

    return records

def run_integrated_scan(url: str) -> list:
    """Entrypoint for FastAPI Background Task"""
    print(f"[*] Starting integrated scan on: {url}")
    # If URL is local file path (for testing)
    path = Path(url)
    if path.exists():
        raw = path.read_text(encoding="utf-8", errors="ignore")
        text = html_to_text(raw) if path.suffix.lower() in {".html", ".htm"} else raw
        return [build_record(str(path), text)]
    else:
        # It's a real URL
        return run_url_mode(url, max_depth=1, max_pages=3)
