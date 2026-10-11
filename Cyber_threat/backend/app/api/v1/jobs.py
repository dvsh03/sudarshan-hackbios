from fastapi import APIRouter, Depends, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from app.models.database import get_db, ScrapeJob, Actor, Entity, InfraFinding
from app.services.extractor import extract_entities_from_text, calculate_sha256
from app.api.v1.graph import MOCK_GRAPH_DB
from app.services.scraper_service import run_integrated_scan

router = APIRouter(prefix="/jobs", tags=["Crawler & Extraction Ops"])

class IngestJobRequest(BaseModel):
    target_url: str
    max_depth: int = 1
    max_pages: int = 10
    delay: float = 2.0
    mode: str = "LIVE_TOR"

class TestTextExtractionRequest(BaseModel):
    raw_content: str

class ScraperDataIngestRequest(BaseModel):
    records: list[dict]

class ScanRequest(BaseModel):
    url: str

from app.models.database import SessionLocal

def background_scan_task(url: str):
    """Runs the scraper and ingests data into the mock DB and SQLite DB. Handles non-URL indicators."""
    
    # If the user enters an indicator (e.g. a Wallet or PGP) instead of a URL
    if not url.startswith("http") and not url.endswith(".html"):
        print(f"[*] Indicator Pivot Search triggered for: {url}")
        # For the demo, dynamically create a Threat Actor based on the search term!
        db = SessionLocal()
        actor_id = url.lower().replace(" ", "_")
        if not db.query(Actor).filter(Actor.id == actor_id).first():
            new_actor = Actor(
                id=actor_id,
                display_handle=f"@{url}",
                category="UNKNOWN",
                risk_level="HIGH",
                origin_badge="PIVOT_DISCOVERY",
                confidence_score=0.75
            )
            db.add(new_actor)
            
            # Give them a random wallet to link
            new_wallet = Entity(
                actor_id=actor_id,
                type="WALLET_BTC",
                value=f"bc1q_{actor_id}_" + str(hash(url))[-8:]
            )
            db.add(new_wallet)
            db.commit()
            
            # Also add to MOCK_GRAPH_DB to render in graph immediately
            global MOCK_GRAPH_DB
            MOCK_GRAPH_DB["nodes"].append({
                "id": actor_id,
                "label": f"@{url}",
                "sublabel": "Discovered Actor",
                "type": "actor",
                "risk": "high"
            })
            wallet_id = new_wallet.value
            MOCK_GRAPH_DB["nodes"].append({
                "id": wallet_id,
                "label": wallet_id[:12] + "...",
                "sublabel": "Unknown Balance",
                "type": "wallet",
                "risk": "medium"
            })
            MOCK_GRAPH_DB["edges"].append({"source": actor_id, "target": wallet_id, "label": "OWNS_WALLET"})
            print(f"[+] Added mock Pivot Actor: {url}")
        db.close()
        return
        
    records = run_integrated_scan(url)
    if records:
        # Connect to DB for background task
        db = SessionLocal()
        req = ScraperDataIngestRequest(records=records)
        ingest_scraper_data(req, db=db)
        db.close()
        print(f"[+] Background scan finished for {url}. Inserted {len(records)} records into Mock DB.")
    else:
        print(f"[-] Background scan failed or found no records for {url}.")

@router.post("/ingest")
def create_ingest_job(req: IngestJobRequest, db: Session = Depends(get_db)):
    job = ScrapeJob(
        target_url=req.target_url,
        mode=req.mode,
        status="COMPLETED", # Synchronous demo completion
        pages_crawled=req.max_pages,
        entities_found=4,
        completed_at=datetime.now(timezone.utc)
    )
    db.add(job)
    db.commit()
    return {
        "success": True,
        "data": {
            "job_id": job.id,
            "target_url": job.target_url,
            "status": "COMPLETED",
            "message": "Tor crawl simulated successfully across safe test mirrors."
        }
    }

@router.post("/extract-preview")
def preview_extraction(req: TestTextExtractionRequest):
    entities = extract_entities_from_text(req.raw_content)
    sha256_hash = calculate_sha256(req.raw_content)
    return {
        "success": True,
        "sha256_integrity_hash": sha256_hash,
        "extracted_entities": entities
    }

@router.post("/ingest_scraper_data")
def ingest_scraper_data(req: ScraperDataIngestRequest, db: Session = Depends(get_db)):
    # Mocking database insertion by using the in-memory store
    global MOCK_GRAPH_DB
    
    total_records = len(req.records)
    print(f"[*] Received {total_records} records from Scraper.")

    for record in req.records:
        source = record.get("source", "Unknown Source")
        identifiers = record.get("identifiers", {})
        
        # 1. Create Actor Nodes from handles
        handles = identifiers.get("handles", [])
        actor_ids = []
        for handle in handles:
            actor_id = handle.lower()
            actor_ids.append(actor_id)
            # Add node if not exists
            if not any(n["id"] == actor_id for n in MOCK_GRAPH_DB["nodes"]):
                MOCK_GRAPH_DB["nodes"].append({
                    "id": actor_id,
                    "label": handle,
                    "sublabel": "Threat Actor",
                    "type": "actor",
                    "risk": "critical"
                })

        # If no handle found, use a default anonymous actor for this source
        if not actor_ids:
            actor_id = "anon_" + str(hash(source))[:6]
            actor_ids.append(actor_id)
            if not any(n["id"] == actor_id for n in MOCK_GRAPH_DB["nodes"]):
                MOCK_GRAPH_DB["nodes"].append({
                    "id": actor_id,
                    "label": "Unknown Actor",
                    "sublabel": source[:20],
                    "type": "actor",
                    "risk": "medium"
                })

        primary_actor = actor_ids[0]
        
        # DB Integration for Actors
        if db:
            for handle_val in handles:
                act_id = handle_val.lower()
                handle_lbl = handle_val
                # Check if exists
                if not db.query(Actor).filter(Actor.id == act_id).first():
                    db.add(Actor(id=act_id, display_handle=handle_lbl, category="UNKNOWN", risk_level="HIGH", origin_badge="SCRAPER"))
                    
            if not handles and actor_ids:
                # Fallback for anonymous
                for act_id in actor_ids:
                    if not db.query(Actor).filter(Actor.id == act_id).first():
                        db.add(Actor(id=act_id, display_handle="Unknown Actor", category="UNKNOWN", risk_level="HIGH", origin_badge="SCRAPER"))

        # 2. Add Wallets
        from app.services.crypto_service import enrich_wallet
        
        for w_type in ["bitcoin_wallets", "ethereum_wallets", "monero_wallets"]:
            for wallet in identifiers.get(w_type, []):
                if not any(n["id"] == wallet for n in MOCK_GRAPH_DB["nodes"]):
                    # Query Blockchain API
                    crypto_name = w_type.split("_")[0]
                    wallet_data = enrich_wallet(wallet, crypto_name)
                    
                    balance_str = wallet_data.get("balance", "Unknown") if wallet_data else "Unknown"
                    tx_count = wallet_data.get("transactions", "?") if wallet_data else "?"
                    
                    MOCK_GRAPH_DB["nodes"].append({
                        "id": wallet,
                        "label": wallet[:12] + "...",
                        "sublabel": f"{balance_str} ({tx_count} TXs)",
                        "type": "wallet",
                        "risk": "high",
                        "wallet_data": wallet_data
                    })
                # Add edge
                MOCK_GRAPH_DB["edges"].append({"source": primary_actor, "target": wallet, "label": "OWNS_WALLET"})
                
                # DB Integration
                if db:
                    wtype = "WALLET_" + w_type.split("_")[0].upper()
                    db.add(Entity(actor_id=primary_actor, type=wtype, value=wallet))

        # 3. Add PGP Keys
        for pgp in identifiers.get("pgp_keys", []):
            pgp_short = pgp.replace("-----BEGIN PGP PUBLIC KEY BLOCK-----", "").strip()[:16]
            if not any(n["id"] == pgp_short for n in MOCK_GRAPH_DB["nodes"]):
                MOCK_GRAPH_DB["nodes"].append({
                    "id": pgp_short,
                    "label": "PGP Key",
                    "sublabel": "RSA 4096",
                    "type": "pgp",
                    "risk": "high"
                })
            MOCK_GRAPH_DB["edges"].append({"source": primary_actor, "target": pgp_short, "label": "USES_KEY"})
            
            # DB Integration
            if db:
                db.add(Entity(actor_id=primary_actor, type="PGP_KEY", value=pgp_short))

        # 4. Add Emails as IPs (for visual variety in mock)
        for email in identifiers.get("emails", []):
            if not any(n["id"] == email for n in MOCK_GRAPH_DB["nodes"]):
                MOCK_GRAPH_DB["nodes"].append({
                    "id": email,
                    "label": email,
                    "sublabel": "Email Address",
                    "type": "ip", # using IP color for emails in this mock
                    "risk": "medium"
                })
            MOCK_GRAPH_DB["edges"].append({"source": primary_actor, "target": email, "label": "USES_EMAIL"})
            
        # 5. Infrastructure Headers & IPs
        from app.services.shodan_service import enrich_ip_with_shodan
        
        for ip in identifiers.get("ipv4_addresses", []):
            if not any(n["id"] == ip for n in MOCK_GRAPH_DB["nodes"]):
                # Hit Shodan API
                shodan_data = enrich_ip_with_shodan(ip)
                country = shodan_data.get("country_name", "Unknown") if shodan_data else "Unknown"
                isp = shodan_data.get("isp", "Unknown ISP") if shodan_data else "Unknown ISP"
                
                MOCK_GRAPH_DB["nodes"].append({
                    "id": ip,
                    "label": ip,
                    "sublabel": f"{country} ({isp[:10]})",
                    "type": "ip",
                    "risk": "critical",
                    "shodan_data": shodan_data
                })
            MOCK_GRAPH_DB["edges"].append({"source": primary_actor, "target": ip, "label": "HOSTED_ON_SERVER"})
            
            if db:
                db.add(Entity(actor_id=primary_actor, type="IPV4_ADDRESS", value=ip))

        headers = record.get("infrastructure", {}).get("server_headers", {})
        server = headers.get("Server")
        if server:
            infra_id = "infra_" + server.replace(" ", "_")
            if not any(n["id"] == infra_id for n in MOCK_GRAPH_DB["nodes"]):
                MOCK_GRAPH_DB["nodes"].append({
                    "id": infra_id,
                    "label": server[:15],
                    "sublabel": "Leaked Server",
                    "type": "ip",
                    "risk": "critical"
                })
            MOCK_GRAPH_DB["edges"].append({"source": primary_actor, "target": infra_id, "label": "HOSTED_ON"})
            
            # DB Integration
            if db:
                db.add(InfraFinding(
                    onion_address=source[:50],
                    finding_type="SERVER_BANNER_LEAK",
                    banner=server,
                    candidate_host=infra_id,
                    strength="STRONG"
                ))
        
        # Add SSL Certificates
        for ssl in identifiers.get("ssl_certificates", []):
            ssl_id = f"ssl_{ssl}"
            if not any(n["id"] == ssl_id for n in MOCK_GRAPH_DB["nodes"]):
                MOCK_GRAPH_DB["nodes"].append({
                    "id": ssl_id,
                    "label": "SSL Cert",
                    "sublabel": f"Serial: {ssl[:8]}...",
                    "type": "ip",
                    "risk": "high"
                })
            MOCK_GRAPH_DB["edges"].append({"source": primary_actor, "target": ssl_id, "label": "USED_SSL_CERT"})

    if db:
        try:
            db.commit()
        except Exception as e:
            db.rollback()
            print("DB Insert Error:", e)

    return {
        "success": True,
        "message": f"Successfully received {total_records} records.",
        "records_processed": total_records
    }

@router.post("/scan")
def start_scan(req: ScanRequest, background_tasks: BackgroundTasks):
    """Triggers the integrated Python scraper in the background."""
    background_tasks.add_task(background_scan_task, req.url)
    return {
        "success": True,
        "message": f"Background scan started for {req.url}. Graph will update automatically."
    }

class StylometryRequest(BaseModel):
    text_a: str
    text_b: str

@router.post("/analyze-style")
def analyze_style(req: StylometryRequest):
    """Uses Gemini API to compare two texts for authorship stylometry."""
    from app.services.gemini_service import analyze_stylometry
    
    result = analyze_stylometry(req.text_a, req.text_b)
    return {
        "success": True,
        "data": result
    }
