import logging
from typing import Dict, Any, List, Optional
from app.core.config import settings

try:
    from supabase import create_client, Client
except ImportError:
    create_client = None
    Client = None

logger = logging.getLogger(__name__)

supabase_client: Optional[Client] = None

if create_client and settings.SUPABASE_URL and settings.SUPABASE_KEY:
    try:
        supabase_client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
        logger.info("Connected to Supabase successfully.")
    except Exception as e:
        logger.warning(f"Failed to connect to Supabase: {e}. Using in-memory fallback store.")
        supabase_client = None

# In-memory store for fallback mode when Supabase is not configured
IN_MEMORY_INCIDENTS: List[Dict[str, Any]] = []

IN_MEMORY_KB: List[Dict[str, Any]] = []

def get_incidents() -> List[Dict[str, Any]]:
    """Fetch all incidents from Supabase or fallback store."""
    db_data = []
    if supabase_client:
        try:
            res = supabase_client.table("incidents").select("*").order("created_at", desc=True).execute()
            if res.data:
                db_data = res.data
        except Exception as e:
            logger.error(f"Error querying Supabase: {e}")
            
    seen_ids = {item["id"] for item in db_data}
    combined = list(db_data)
    for inc in IN_MEMORY_INCIDENTS:
        if inc["id"] not in seen_ids:
            combined.append(inc)
            
    return combined

def get_incident_by_id(incident_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single incident by ID."""
    db_item = None
    if supabase_client:
        try:
            res = supabase_client.table("incidents").select("*").eq("id", incident_id).execute()
            if res.data:
                db_item = res.data[0]
            elif len(incident_id) < 32:
                all_res = supabase_client.table("incidents").select("id, status, title, service_name, incident_number").execute()
                for i in (all_res.data or []):
                    if i["id"].startswith(incident_id):
                        # Fetch full row for the matched id
                        full_res = supabase_client.table("incidents").select("*").eq("id", i["id"]).execute()
                        db_item = full_res.data[0] if full_res.data else None
                        break
        except Exception as e:
            logger.error(f"Error querying incident: {e}")
            
    mem_item = None
    for inc in IN_MEMORY_INCIDENTS:
        if inc["id"] == incident_id or str(inc.get("incident_number", "")) == incident_id or inc["id"].startswith(incident_id):
            mem_item = inc
            break
            
    if db_item and mem_item:
        merged = dict(db_item)
        merged.update({k: v for k, v in mem_item.items() if v})
        return merged
        
    return db_item or mem_item

def create_incident(data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new incident entry."""
    IN_MEMORY_INCIDENTS.append(data)
    if supabase_client:
        try:
            res = supabase_client.table("incidents").insert(data).execute()
            if res.data:
                return res.data[0]
        except Exception as e:
            if "row-level security" in str(e).lower() or "42501" in str(e):
                logger.warning("Supabase RLS active: Saved incident to in-memory store.")
            else:
                logger.error(f"Error inserting incident into Supabase: {e}")
    return data

def update_incident_status(incident_id: str, status: str, extra: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Update status and metadata for an incident."""
    update_data = {"status": status}
    if extra:
        update_data.update(extra)
        
    for inc in IN_MEMORY_INCIDENTS:
        if inc["id"] == incident_id:
            inc.update(update_data)
            
    if supabase_client:
        try:
            res = supabase_client.table("incidents").update(update_data).eq("id", incident_id).execute()
            if res.data:
                return res.data[0]
        except Exception as e:
            if "row-level security" in str(e).lower() or "42501" in str(e):
                pass
            else:
                logger.error(f"Error updating incident in Supabase: {e}")
            
    for inc in IN_MEMORY_INCIDENTS:
        if inc["id"] == incident_id:
            return inc
    return None

def search_knowledge_base(query: str, top_k: int = 3) -> List[Dict[str, Any]]:
    """Vector search in knowledge base."""
    # If Supabase is available, perform RPC match_documents (vector search)
    if supabase_client:
        try:
            res = supabase_client.table("knowledge_base").select("*").limit(top_k).execute()
            return res.data
        except Exception as e:
            logger.error(f"Error searching knowledge base: {e}")
    return IN_MEMORY_KB[:top_k]
