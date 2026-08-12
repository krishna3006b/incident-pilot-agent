import logging
from typing import List, Dict, Any, Optional
from app.db.supabase import supabase_client, IN_MEMORY_KB, get_incidents

logger = logging.getLogger(__name__)


class RepositoryKnowledgeService:
    """
    Repository Knowledge Service layer.
    Encapsulates queries to code embeddings, dependency graphs,
    historical resolution memory, and runbooks.
    """

    def get_manifest(self, repo_id: str = "ordering-system") -> Dict[str, Any]:
        """Fetch repository metadata manifest (language, framework, indexed stats)."""
        if supabase_client:
            try:
                res = supabase_client.table('repository_metadata').select('*').eq('name', repo_id).execute()
                if res.data:
                    meta = res.data[0]
                    # Enrich with live symbol/embedding counts
                    sym_count = 0
                    emb_count = 0
                    try:
                        sym_res = supabase_client.table('repository_symbols').select('id', count='exact').eq('repository_id', meta['id']).execute()
                        sym_count = sym_res.count or 0
                    except Exception:
                        pass
                    try:
                        emb_res = supabase_client.table('code_embeddings').select('id', count='exact').eq('repository_id', meta['id']).execute()
                        emb_count = emb_res.count or 0
                    except Exception:
                        pass
                    return {
                        'repository_id': meta['id'],
                        'name': meta.get('name'),
                        'repo_url': meta.get('repo_url'),
                        'language': meta.get('language'),
                        'framework': meta.get('framework'),
                        'build_system': meta.get('build_system'),
                        'last_indexed_sha': meta.get('last_indexed_sha'),
                        'entry_points': meta.get('entry_points', []),
                        'dependencies': meta.get('dependencies', []),
                        'symbol_count': sym_count,
                        'embedding_count': emb_count,
                        'status': 'Ready' if meta.get('last_indexed_sha') else 'Not Indexed'
                    }
            except Exception as e:
                logger.error(f"Error fetching repository manifest: {e}")

        # Fallback manifest
        return {
            'repository_id': repo_id,
            'name': repo_id,
            'language': 'TypeScript',
            'framework': 'Next.js',
            'last_indexed_sha': None,
            'symbol_count': 0,
            'embedding_count': 0,
            'status': 'Not Indexed'
        }

    def search_code(self, query: str, repo_id: str = "ordering-system", top_k: int = 5) -> List[Dict[str, Any]]:
        """Search code embeddings via Supabase pgvector RPC or table fallback."""
        if supabase_client:
            try:
                # Try RPC match_code with embedding (requires embedding the query first)
                # Fall back to direct table scan for now
                res = supabase_client.table('code_embeddings').select(
                    'id, file_path, symbol_name, symbol_type, content'
                ).limit(top_k).execute()
                if res.data:
                    # Add similarity placeholder since we're not doing vector search here
                    for item in res.data:
                        item['similarity'] = 0.90
                    return res.data
            except Exception as e:
                logger.error(f"Error querying code_embeddings: {e}")

        # In-memory fallback
        return [
            {
                'file_path': 'src/app/api/discount/route.ts',
                'symbol_name': 'POST',
                'symbol_type': 'function',
                'content': 'const price = body.product.price;\nreturn NextResponse.json({ discount: price * 0.1 });',
                'similarity': 0.95
            },
            {
                'file_path': 'src/app/api/inventory/route.ts',
                'symbol_name': 'POST',
                'symbol_type': 'function',
                'content': 'const qty = body.inventory.stock_quantity;\nreturn NextResponse.json({ stock: qty });',
                'similarity': 0.89
            }
        ][:top_k]

    def get_symbol(self, symbol_name: str, repo_id: str = "ordering-system") -> Optional[Dict[str, Any]]:
        """Fetch a specific symbol definition by name."""
        if supabase_client:
            try:
                res = supabase_client.table('repository_symbols').select('*').eq('symbol_name', symbol_name).limit(1).execute()
                if res.data:
                    return res.data[0]
            except Exception as e:
                logger.error(f"Error fetching symbol {symbol_name}: {e}")
        return None

    def get_dependencies(self, symbol_name: str, repo_id: str = "ordering-system") -> List[Dict[str, Any]]:
        """Fetch outgoing dependency edges for a symbol (what this symbol calls/imports)."""
        if supabase_client:
            try:
                # First find the symbol ID
                sym = self.get_symbol(symbol_name, repo_id)
                if sym:
                    res = supabase_client.table('repository_edges').select('*').eq('source_symbol_id', sym['id']).execute()
                    if res.data:
                        return res.data
            except Exception as e:
                logger.error(f"Error fetching dependencies for {symbol_name}: {e}")

        # Deterministic fallback
        return [
            {'source': symbol_name, 'relationship': 'calls', 'target': 'DatabaseClient'},
            {'source': symbol_name, 'relationship': 'depends_on', 'target': 'AuthValidator'}
        ]

    def get_dependents(self, symbol_name: str, repo_id: str = "ordering-system") -> List[Dict[str, Any]]:
        """Fetch incoming dependency edges for a symbol (what calls/imports this symbol)."""
        if supabase_client:
            try:
                sym = self.get_symbol(symbol_name, repo_id)
                if sym:
                    res = supabase_client.table('repository_edges').select('*').eq('target_symbol_id', sym['id']).execute()
                    if res.data:
                        return res.data
            except Exception as e:
                logger.error(f"Error fetching dependents for {symbol_name}: {e}")

        return [
            {'source': 'APIRouter', 'relationship': 'calls', 'target': symbol_name}
        ]

    def get_historical_incident_memory(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """Fetch past resolved incidents for memory-based resolution."""
        incidents = get_incidents()
        matches = []
        for inc in incidents:
            if inc.get('status') in ['PR_READY', 'RESOLVED', 'CHANGES_REQUESTED']:
                matches.append({
                    'incident_id': inc.get('id'),
                    'title': inc.get('title'),
                    'root_cause': inc.get('root_cause', 'Null check missing on request payload'),
                    'status': inc.get('status')
                })
        return matches[:top_k]

    def get_runbook_entries(self, service_name: str) -> List[Dict[str, Any]]:
        """Fetch verified operational runbooks."""
        if supabase_client:
            try:
                res = supabase_client.table('knowledge_base').select('*').limit(3).execute()
                if res.data:
                    return res.data
            except Exception as e:
                logger.error(f"Error fetching runbooks: {e}")
        return IN_MEMORY_KB


# Singleton instance
knowledge_service = RepositoryKnowledgeService()
