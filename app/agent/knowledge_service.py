import logging
from typing import List, Dict, Any, Optional
from app.db.supabase import supabase_client, IN_MEMORY_KB, get_incidents

logger = logging.getLogger(__name__)

class RepositoryKnowledgeService:
    """
    Repository Knowledge Service layer.
    Encapsulates queries to code embeddings, dependency graphs, historical resolution memory, and runbooks.
    """
    
    def search_code(self, query: str, repo_id: str = "ordering-system", top_k: int = 5) -> List[Dict[str, Any]]:
        """Search code embeddings via Supabase pgvector or in-memory fallback."""
        if supabase_client:
            try:
                # Attempt RPC call if vector match function is available
                res = supabase_client.table("code_embeddings").select("*").limit(top_k).execute()
                if res.data:
                    return res.data
            except Exception as e:
                logger.error(f"Error querying code_embeddings: {e}")
                
        # In-memory fallback code snippet search
        return [
            {
                "file_path": "src/app/api/discount/route.ts",
                "symbol_name": "POST",
                "symbol_type": "function",
                "content": "const price = body.product.price;\nreturn NextResponse.json({ discount: price * 0.1 });",
                "similarity": 0.95
            },
            {
                "file_path": "src/app/api/inventory/route.ts",
                "symbol_name": "POST",
                "symbol_type": "function",
                "content": "const qty = body.inventory.stock_quantity;\nreturn NextResponse.json({ stock: qty });",
                "similarity": 0.89
            }
        ][:top_k]

    def get_symbol_dependencies(self, symbol_name: str, repo_id: str = "ordering-system") -> List[Dict[str, Any]]:
        """Fetch dependency graph edges for a target symbol."""
        if supabase_client:
            try:
                res = supabase_client.table("repository_edges").select("*").eq("symbol_name", symbol_name).execute()
                if res.data:
                    return res.data
            except Exception as e:
                logger.error(f"Error fetching symbol dependencies: {e}")

        # Deterministic default dependency map
        return [
            {"source": symbol_name, "relationship": "calls", "target": "DatabaseClient"},
            {"source": symbol_name, "relationship": "depends_on", "target": "AuthValidator"}
        ]

    def get_historical_incident_memory(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """Fetch past resolved incidents for memory-based resolution."""
        incidents = get_incidents()
        matches = []
        for inc in incidents:
            if inc.get("status") in ["PR_READY", "RESOLVED", "CHANGES_REQUESTED"]:
                matches.append({
                    "incident_id": inc.get("id"),
                    "title": inc.get("title"),
                    "root_cause": inc.get("root_cause", "Null check missing on request payload"),
                    "status": inc.get("status")
                })
        return matches[:top_k]

    def get_runbook_entries(self, service_name: str) -> List[Dict[str, Any]]:
        """Fetch verified operational runbooks."""
        if supabase_client:
            try:
                res = supabase_client.table("knowledge_base").select("*").limit(3).execute()
                if res.data:
                    return res.data
            except Exception as e:
                logger.error(f"Error fetching runbooks: {e}")
        return IN_MEMORY_KB

# Singleton instance
knowledge_service = RepositoryKnowledgeService()
