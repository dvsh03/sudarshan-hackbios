import argparse
import csv
import json
import re
import sys
import time
import hashlib
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

TIMEOUT_SECONDS = 45

PATTERNS = {
    "bitcoin_wallets": re.compile(
        r"\b(?:[13][a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[a-zA-HJ-NP-Z0-9]{39,59})\b"
    ),
    "ethereum_wallets": re.compile(r"\b0x[a-fA-F0-9]{40}\b"),
    "monero_wallets": re.compile(r"\b[48][0-9ABa-zA-Z]{94}\b"),
    "emails": re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    "pgp_keys": re.compile(
        r"-----BEGIN PGP PUBLIC KEY BLOCK-----[\s\S]*?-----END PGP PUBLIC KEY BLOCK-----"
    ),
    "onion_links": re.compile(r"\b[a-z2-7]{56}\.onion\b", re.IGNORECASE),
    "handles": re.compile(r"(?:Author|User|Username|Profile|Member):\s*([a-zA-Z0-9_-]{3,20})|@([a-zA-Z0-9_-]{3,20})", re.IGNORECASE),
    "ipv4_addresses": re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")
}

SUPPORTED_LOCAL_EXTENSIONS = {".html", ".htm", ".txt", ".json", ".tsv", ".csv"}


def extract_entities(text: str) -> dict:
    return {
        key: sorted(set(pattern.findall(text)))
        for key, pattern in PATTERNS.items()
    }


def build_record(source: str, text: str, headers: dict = None, sha256_hash: str = None) -> dict:
    entities = extract_entities(text)
    
    # Flatten handles since regex returns tuples due to multiple capture groups
    if "handles" in entities:
        flat_handles = []
        for match in entities["handles"]:
            # extract whichever group matched
            flat_handles.extend([m for m in match if m])
        entities["handles"] = sorted(set(flat_handles))

    total_identifiers = sum(len(v) for v in entities.values())

    return {
        "source": source,
        "last_scan_date": datetime.now(timezone.utc).isoformat(),
        "category": "unclassified",
        "attribution_confidence": None,
        "sha256_hash": sha256_hash,
        "identifiers": entities,
        "identifier_count": total_identifiers,
        "infrastructure": {
            "server_headers": headers or {}
        }
    }


def html_to_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    # Remove script and style elements to avoid junk text
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


def fetch_url(url: str, max_retries: int = 3) -> tuple[str, str, dict, str] | tuple[None, None, None, None]:
    print(f"[*] Connecting to {url} via Tor...")
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(
                url, proxies=PROXIES, headers=HEADERS, timeout=TIMEOUT_SECONDS
            )
            if response.status_code == 200:
                print(f"[+] Connected (Attempt {attempt}). Status 200, {len(response.content)} bytes received.")
                
                # Extract important security/server headers
                important_headers = {}
                for key in ["Server", "X-Powered-By", "Via"]:
                    if key in response.headers:
                        important_headers[key] = response.headers[key]
                        
                raw_text = html_to_text(response.text)
                digest = hashlib.sha256(response.content).hexdigest()
                return raw_text, response.text, important_headers, digest
            else:
                print(f"[-] Unexpected status code {response.status_code} on attempt {attempt}.")
                
        except requests.exceptions.RequestException as e:
            print(f"[-] Request failed on attempt {attempt}: {e}")
        
        if attempt < max_retries:
            print(f"[*] Retrying in {attempt * 3} seconds...")
            time.sleep(attempt * 3)

    print(f"[-] Max retries ({max_retries}) reached for {url}. Skipping.")
    return None, None, None, None


def run_url_mode(
    start_url: str,
    max_depth: int = 0,
    max_pages: int = 1,
    same_host_only: bool = True,
    delay_seconds: float = 2.0,
) -> list:
    visited = set()
    queue = deque([(start_url, 0)])
    records = []

    while queue and len(visited) < max_pages:
        url, depth = queue.popleft()
        if url in visited:
            continue
        visited.add(url)

        text, raw_html, headers, digest = fetch_url(url)
        if text is None:
            continue

        record = build_record(url, text, headers, digest)
        record["crawl_depth"] = depth
        records.append(record)
        print(f"    [+] {url} (depth {depth}): {record['identifier_count']} identifiers found")

        if depth < max_depth and len(visited) < max_pages:
            for link in extract_links(url, raw_html, same_host_only):
                if link not in visited:
                    queue.append((link, depth + 1))

        if queue:
            time.sleep(delay_seconds)

    return records


def read_local_file(path: Path) -> tuple[str, str]:
    raw_bytes = path.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    raw = raw_bytes.decode("utf-8", errors="ignore")
    if path.suffix.lower() in {".html", ".htm"}:
        return html_to_text(raw), digest
    return raw, digest


def run_local_mode(input_path: Path) -> list:
    if not input_path.exists():
        print(f"[-] Path does not exist: {input_path}")
        sys.exit(1)

    if input_path.is_file():
        files = [input_path]
    else:
        files = [
            p for p in input_path.rglob("*")
            if p.suffix.lower() in SUPPORTED_LOCAL_EXTENSIONS
        ]

    if not files:
        print(f"[-] No supported files found ({SUPPORTED_LOCAL_EXTENSIONS}) in {input_path}")
        sys.exit(1)

    print(f"[*] Processing {len(files)} file(s)...")
    records = []
    for f in files:
        try:
            text, digest = read_local_file(f)
            record = build_record(str(f), text, sha256_hash=digest)
            records.append(record)
            print(f"    [+] {f.name}: {record['identifier_count']} identifiers found")
        except Exception as e:
            print(f"    [-] {f.name}: skipped, error: {e}")

    return records


def save_json(records: list, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=4, ensure_ascii=False)
    print(f"[+] JSON saved -> {path}")


def save_csv(records: list, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["source", "last_scan_date", "category", "crawl_depth", "entity_type", "value"])
        for record in records:
            depth = record.get("crawl_depth", "")
            for entity_type, values in record["identifiers"].items():
                for value in values:
                    writer.writerow(
                        [record["source"], record["last_scan_date"], record["category"], depth, entity_type, value]
                    )
    print(f"[+] CSV saved -> {path}")


def upload_to_api(records: list, api_url: str) -> None:
    print(f"[*] Uploading {len(records)} records to {api_url} ...")
    try:
        response = requests.post(api_url, json={"records": records}, timeout=10)
        if response.status_code in (200, 201):
            print("[+] Successfully ingested data to Backend API!")
        else:
            print(f"[-] Backend API rejected data (Status {response.status_code}): {response.text}")
    except requests.exceptions.RequestException as e:
        print(f"[-] Failed to connect to Backend API: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Dark web threat intelligence entity extractor (Tor live URL OR local dataset)"
    )

    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--url", help="Live .onion/clearnet URL (will be fetched through Tor)")
    mode_group.add_argument("--local", help="Local file or folder path (archived dataset)")

    parser.add_argument(
        "--max-depth",
        type=int,
        default=0,
        help="[--url mode only] How many levels deep to recursively crawl. "
             "0 = only the start URL (default, old behaviour). 1 = start URL + its direct links, etc.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=10,
        help="[--url mode only] Total number of pages to fetch (safety cap, default: 10).",
    )
    parser.add_argument(
        "--allow-cross-domain",
        action="store_true",
        help="[--url mode only] By default the crawler stays within the same .onion/domain. "
             "This flag allows following links to other domains as well (careful: OpSec risk).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="[--url mode only] Delay in seconds between each request (to avoid rate-limiting/bans, default: 2.0).",
    )
    parser.add_argument(
        "--outdir",
        default="output",
        help="Where to save JSON/CSV files (default: ./output)",
    )
    parser.add_argument(
        "--api-url",
        help="FastAPI ingest endpoint to send data to (e.g., http://localhost:8000/api/v1/jobs/ingest_scraper_data)",
    )
    args = parser.parse_args()

    if args.url:
        records = run_url_mode(
            args.url,
            max_depth=args.max_depth,
            max_pages=args.max_pages,
            same_host_only=not args.allow_cross_domain,
            delay_seconds=args.delay,
        )
        file_prefix = "url_intel"
    else:
        records = run_local_mode(Path(args.local))
        file_prefix = "local_intel"

    total_identifiers = sum(r["identifier_count"] for r in records)
    print(f"\n[+] Done. Total records: {len(records)}, Total identifiers: {total_identifiers}")

    if len(records) == 1:
        print(json.dumps(records[0], indent=4, ensure_ascii=False))

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    timestamp_tag = datetime.now().strftime("%Y%m%d_%H%M%S")

    save_json(records, outdir / f"{file_prefix}_{timestamp_tag}.json")
    save_csv(records, outdir / f"{file_prefix}_{timestamp_tag}.csv")
    
    if args.api_url:
        upload_to_api(records, args.api_url)


if __name__ == "__main__":
    main()