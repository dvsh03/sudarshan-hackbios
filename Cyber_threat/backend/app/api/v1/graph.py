from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.models.database import get_db, Actor, Entity, InfraFinding, CorrelationLink
import uuid

router = APIRouter(prefix="/graph", tags=["Graph Visualizer"])

# IN-MEMORY MOCK DB FOR TESTING SCRAPER
MOCK_GRAPH_DB = {
    "nodes": [],
    "edges": []
}

@router.delete("/clear")
def clear_graph_data():
    global MOCK_GRAPH_DB
    MOCK_GRAPH_DB["nodes"].clear()
    MOCK_GRAPH_DB["edges"].clear()
    return {"success": True, "message": "Memory cleared"}

@router.get("/data")
def get_graph_data(db: Session = Depends(get_db)):
    nodes = []
    edges = []
    
    # 1. Add Actors
    actors = db.query(Actor).all()
    for a in actors:
        nodes.append({
            "id": a.id,
            "label": a.display_handle,
            "type": "actor"
        })
        
    # 2. Add Entities and link to Actors
    entities = db.query(Entity).all()
    for e in entities:
        node_type = "unknown"
        if "WALLET" in e.type:
            node_type = "wallet"
        elif "PGP" in e.type:
            node_type = "pgp"
        elif "IP" in e.type:
            node_type = "ip"
            
        # Add entity node if not exists
        if not any(n["id"] == e.value for n in nodes):
            # Try to shorten label
            label = e.value
            if node_type == "pgp" and len(label) > 15:
                label = f"Version: G..." # matching UI format for long keys, or just short
            elif node_type == "wallet" and len(label) > 20:
                label = label[:16] + "..."
                
            nodes.append({
                "id": e.value,
                "label": label,
                "type": node_type
            })
            
        # Add edge from actor to entity
        edges.append({
            "source": e.actor_id,
            "target": e.value,
            "label": f"HAS_{node_type.upper()}"
        })
        
    # 3. Add CorrelationLinks
    correlations = db.query(CorrelationLink).all()
    for link in correlations:
        edges.append({
            "source": link.actor_a_id,
            "target": link.actor_b_id,
            "label": link.vector
        })

    # Combine with MOCK_GRAPH_DB (if any)
    for n in MOCK_GRAPH_DB["nodes"]:
        if not any(node["id"] == n["id"] for node in nodes):
            nodes.append(n)
            
    for e in MOCK_GRAPH_DB["edges"]:
        # check for duplicates
        if not any(edge["source"] == e["source"] and edge["target"] == e["target"] for edge in edges):
            edges.append(e)

    return {
        "success": True,
        "data": {
            "nodes": nodes,
            "edges": edges,
            "counts": {
                "total_nodes": len(nodes),
                "total_edges": len(edges)
            }
        }
    }

@router.get("/nodes/{node_id}")
def get_node_details(node_id: str, db: Session = Depends(get_db)):
    # Try DB first
    actor = db.query(Actor).filter(Actor.id == node_id).first()
    if actor:
        return {"success": True, "data": {"id": actor.id, "label": actor.display_handle, "type": "actor"}}
    
    entity = db.query(Entity).filter(Entity.value == node_id).first()
    if entity:
        return {"success": True, "data": {"id": entity.value, "label": entity.value, "type": entity.type}}
        
    node = next((n for n in MOCK_GRAPH_DB["nodes"] if n["id"] == node_id), None)
    if not node:
        return {"success": False, "message": "Node not found"}
    return {"success": True, "data": node}
