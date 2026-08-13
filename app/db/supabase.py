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
        logger.error(f"Failed to connect to Supabase: {e}")
        raise RuntimeError(f"Database connection failed: {e}")
else:
    logger.error("SUPABASE_URL or SUPABASE_KEY is missing from environment variables.")
    raise RuntimeError("Missing database configuration.")

def _get_client() -> Client:
    if not supabase_client:
        raise RuntimeError("Supabase client is not initialized.")
    return supabase_client

def get_incidents() -> List[Dict[str, Any]]:
    """Fetch all incidents from Supabase."""
    client = _get_client()
    res = client.table("incidents").select("*").order("created_at", desc=True).execute()
    return res.data if res.data else []

def get_incident_by_id(incident_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single incident by ID."""
    client = _get_client()
    if len(incident_id) < 32:
        # Prefix match
        all_res = client.table("incidents").select("id").execute()
        for i in (all_res.data or []):
            if i["id"].startswith(incident_id):
                full_res = client.table("incidents").select("*").eq("id", i["id"]).execute()
                return full_res.data[0] if full_res.data else None
        return None
    else:
        res = client.table("incidents").select("*").eq("id", incident_id).execute()
        return res.data[0] if res.data else None

def create_incident(data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new incident entry."""
    client = _get_client()
    res = client.table("incidents").insert(data).execute()
    if res.data:
        return res.data[0]
    raise RuntimeError("Failed to insert incident")

def update_incident_status(incident_id: str, status: str, extra: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Update status and metadata for an incident."""
    update_data = {"status": status}
    if extra:
        update_data.update(extra)
        
    client = _get_client()
    res = client.table("incidents").update(update_data).eq("id", incident_id).execute()
    return res.data[0] if res.data else None

def create_sandbox_run(data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new sandbox execution record."""
    client = _get_client()
    res = client.table("sandbox_runs").insert(data).execute()
    if res.data:
        return res.data[0]
    raise RuntimeError("Failed to insert sandbox run")

def get_sandbox_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single sandbox run by ID."""
    client = _get_client()
    res = client.table("sandbox_runs").select("*").eq("id", run_id).execute()
    return res.data[0] if res.data else None

def update_sandbox_run(run_id: str, update_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Update status and metadata for a sandbox run."""
    client = _get_client()
    res = client.table("sandbox_runs").update(update_data).eq("id", run_id).execute()
    return res.data[0] if res.data else None

def search_knowledge_base(query: str, top_k: int = 3) -> List[Dict[str, Any]]:
    """Vector search in knowledge base."""
    client = _get_client()
    res = client.table("knowledge_base").select("*").limit(top_k).execute()
    return res.data if res.data else []
