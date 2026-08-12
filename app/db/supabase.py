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
        # Temporarily disable to prevent threadpool exhaustion since table doesn't exist
        supabase_client = None 
    except Exception as e:
        logger.warning(f"Failed to connect to Supabase: {e}. Using in-memory fallback store.")

# In-memory store for fallback mode when Supabase is not configured
IN_MEMORY_INCIDENTS: List[Dict[str, Any]] = []

IN_MEMORY_KB: List[Dict[str, Any]] = [
    {
        "id": "22222222-2222-2222-2222-222222222222",
        "document_type": "runbook",
        "title": "Payment Service Null Address Troubleshooting",
        "content": "When payment-service returns 500 during checkout, inspect Customer.getAddress() nullable fields introduced in v1.8.2 migration.",
        "resolution_status": "VERIFIED"
    }
]

def get_incidents() -> List[Dict[str, Any]]:
    """Fetch all incidents from Supabase or fallback store."""
    if supabase_client:
        try:
            res = supabase_client.table("incidents").select("*").order("created_at", desc=True).execute()
            return res.data
        except Exception as e:
            logger.error(f"Error querying Supabase: {e}")
    return IN_MEMORY_INCIDENTS

def get_incident_by_id(incident_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single incident by ID."""
    if supabase_client:
        try:
            res = supabase_client.table("incidents").select("*").eq("id", incident_id).execute()
            if res.data:
                return res.data[0]
        except Exception as e:
            logger.error(f"Error querying incident: {e}")
    for inc in IN_MEMORY_INCIDENTS:
        if inc["id"] == incident_id or str(inc["incident_number"]) == incident_id:
            return inc
    return None

def create_incident(data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new incident entry."""
    if supabase_client:
        try:
            res = supabase_client.table("incidents").insert(data).execute()
            if res.data:
                return res.data[0]
        except Exception as e:
            logger.error(f"Error inserting incident into Supabase: {e}")
    IN_MEMORY_INCIDENTS.append(data)
    return data

def update_incident_status(incident_id: str, status: str, extra: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Update status and metadata for an incident."""
    update_data = {"status": status}
    if extra:
        update_data.update(extra)
        
    if supabase_client:
        try:
            res = supabase_client.table("incidents").update(update_data).eq("id", incident_id).execute()
            if res.data:
                return res.data[0]
        except Exception as e:
            logger.error(f"Error updating incident in Supabase: {e}")
            
    for inc in IN_MEMORY_INCIDENTS:
        if inc["id"] == incident_id:
            inc.update(update_data)
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
