import os
import re
import uuid
import json
import logging
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from app.db.supabase import supabase_client

logger = logging.getLogger(__name__)

# --- Tree-sitter Setup (with graceful fallback) ---
_TREE_SITTER_AVAILABLE = False
_ts_parsers = {}

try:
    from tree_sitter import Language, Parser
    try:
        import tree_sitter_javascript as ts_js
        import tree_sitter_typescript as ts_ts
        _ts_parsers['javascript'] = Parser(Language(ts_js.language()))
        _ts_parsers['typescript'] = Parser(Language(ts_ts.language_typescript()))
        _ts_parsers['tsx'] = Parser(Language(ts_ts.language_tsx()))
        _TREE_SITTER_AVAILABLE = True
        logger.info("Tree-sitter initialized for JavaScript/TypeScript.")
    except ImportError:
        logger.info("Tree-sitter language grammars not installed. Using regex fallback.")
    try:
        import tree_sitter_python as ts_py
        _ts_parsers['python'] = Parser(Language(ts_py.language()))
    except ImportError:
        pass
    try:
        import tree_sitter_java as ts_java
        _ts_parsers['java'] = Parser(Language(ts_java.language()))
    except ImportError:
        pass
except ImportError:
    logger.info("tree-sitter package not installed. Using regex-based AST parsing.")


from app.services.embedding import generate_embeddings


# --- Extension to Language Mapping ---
EXT_LANG_MAP = {
    '.ts': 'typescript', '.tsx': 'tsx',
    '.js': 'javascript', '.jsx': 'javascript',
    '.py': 'python', '.java': 'java',
}


# --- Repository Indexer & AST Parser ---
class RepositoryIndexer:
    def __init__(
        self,
        repo_path: str,
        repo_id: str,
        commit_sha: str = "HEAD"
    ):
        self.repo_path = repo_path
        self.repo_id = repo_id
        self.commit_sha = commit_sha

    def _extract_edges(self, file_path: str, content: str) -> List[Dict[str, Any]]:
        """Extract simple dependency edges (imports/requires)."""
        edges = []
        lines = content.splitlines()
        
        # Matches: import X from 'Y', import 'Y', require('Y'), from Y import X
        import_pattern = re.compile(r'(?:import.*?from\s+[\'"](.*?)[\'"])|(?:import\s+[\'"](.*?)[\'"])|(?:require\([\'"](.*?)[\'"]\))|(?:from\s+([A-Za-z0-9_\.]+)\s+import)')
        
        for line in lines:
            match = import_pattern.search(line)
            if match:
                target = next(g for g in match.groups() if g is not None)
                edges.append({
                    'id': str(uuid.uuid4()),
                    'repository_id': self.repo_id,
                    'source_symbol': os.path.basename(file_path),
                    'target_symbol': target,
                    'edge_type': 'imports',
                    'weight': 1.0
                })
        return edges

    # ----- Tree-sitter AST Parsing -----
    def _parse_with_tree_sitter(self, file_path: str, content: str, lang: str) -> List[Dict[str, Any]]:
        """Extract symbols using Tree-sitter AST queries."""
        parser = _ts_parsers.get(lang)
        if not parser:
            return []

        tree = parser.parse(bytes(content, 'utf8'))
        root = tree.root_node
        symbols = []
        lines = content.splitlines()

        def _walk(node):
            # Capture classes
            if node.type in ('class_declaration', 'class_definition'):
                name_node = node.child_by_field_name('name')
                if name_node:
                    start = node.start_point[0]
                    end = node.end_point[0]
                    symbols.append({
                        'symbol_name': name_node.text.decode('utf8'),
                        'symbol_type': 'class',
                        'file_path': file_path,
                        'start_line': start + 1,
                        'end_line': end + 1,
                        'content': '\n'.join(lines[start:end + 1]),
                        'signature': lines[start].strip() if start < len(lines) else ''
                    })

            # Capture functions / methods
            elif node.type in (
                'function_declaration', 'function_definition',
                'method_definition', 'arrow_function',
                'lexical_declaration',  # const x = async () => {}
            ):
                name_node = node.child_by_field_name('name')
                # For lexical_declaration, dig into the declarator
                if not name_node and node.type == 'lexical_declaration':
                    for child in node.children:
                        if child.type == 'variable_declarator':
                            name_node = child.child_by_field_name('name')
                            break

                if name_node:
                    start = node.start_point[0]
                    end = node.end_point[0]
                    sym_type = 'method' if node.type == 'method_definition' else 'function'
                    symbols.append({
                        'symbol_name': name_node.text.decode('utf8'),
                        'symbol_type': sym_type,
                        'file_path': file_path,
                        'start_line': start + 1,
                        'end_line': end + 1,
                        'content': '\n'.join(lines[start:end + 1]),
                        'signature': lines[start].strip() if start < len(lines) else ''
                    })

            # Capture exported functions (export async function POST)
            elif node.type == 'export_statement':
                for child in node.children:
                    if child.type in ('function_declaration', 'lexical_declaration'):
                        _walk(child)
                return  # Don't recurse into children again

            # Capture interfaces (TypeScript)
            elif node.type in ('interface_declaration', 'type_alias_declaration'):
                name_node = node.child_by_field_name('name')
                if name_node:
                    start = node.start_point[0]
                    end = node.end_point[0]
                    symbols.append({
                        'symbol_name': name_node.text.decode('utf8'),
                        'symbol_type': 'interface',
                        'file_path': file_path,
                        'start_line': start + 1,
                        'end_line': end + 1,
                        'content': '\n'.join(lines[start:end + 1]),
                        'signature': lines[start].strip() if start < len(lines) else ''
                    })

            for child in node.children:
                _walk(child)

        _walk(root)
        return symbols

    # ----- Regex Fallback AST Parsing -----
    def _parse_with_regex(self, file_path: str, content: str) -> List[Dict[str, Any]]:
        """Fallback regex-based symbol extraction."""
        symbols = []
        lines = content.splitlines()

        class_pattern = re.compile(r'^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z0-9_]+)')
        func_pattern = re.compile(r'^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z0-9_]+)')
        const_func_pattern = re.compile(r'^\s*(?:export\s+)?const\s+([A-Za-z0-9_]+)\s*=\s*(?:async\s*)?\(')
        py_func_pattern = re.compile(r'^\s*(?:async\s+)?def\s+([A-Za-z0-9_]+)\s*\(')
        py_class_pattern = re.compile(r'^\s*class\s+([A-Za-z0-9_]+)')
        java_method_pattern = re.compile(r'^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:\w+\s+)+([A-Za-z0-9_]+)\s*\(')

        for idx, line in enumerate(lines):
            match = (
                class_pattern.search(line) or
                func_pattern.search(line) or
                const_func_pattern.search(line) or
                py_func_pattern.search(line) or
                py_class_pattern.search(line) or
                java_method_pattern.search(line)
            )
            if match:
                sym_type = 'class' if 'class ' in line.lower() else 'function'
                end_line = min(idx + 30, len(lines))
                symbols.append({
                    'symbol_name': match.group(1),
                    'symbol_type': sym_type,
                    'file_path': file_path,
                    'start_line': idx + 1,
                    'end_line': end_line,
                    'content': '\n'.join(lines[idx:end_line]),
                    'signature': line.strip()
                })

        return symbols

    def parse_ast_symbols(self, file_path: str, content: str) -> List[Dict[str, Any]]:
        """
        Parses classes, functions, methods, and interfaces from code content.
        Uses Tree-sitter if available, with a regex fallback.
        """
        ext = os.path.splitext(file_path)[1].lower()
        lang = EXT_LANG_MAP.get(ext, '')

        symbols = []
        if _TREE_SITTER_AVAILABLE and lang in _ts_parsers:
            symbols = self._parse_with_tree_sitter(file_path, content, lang)
            if symbols:
                logger.debug(f"Tree-sitter extracted {len(symbols)} symbols from {file_path}")

        if not symbols:
            symbols = self._parse_with_regex(file_path, content)

        # If no symbols found, treat whole file as a module symbol
        if not symbols:
            symbols.append({
                'symbol_name': os.path.basename(file_path),
                'symbol_type': 'module',
                'file_path': file_path,
                'start_line': 1,
                'end_line': len(content.splitlines()),
                'content': content[:2000],
                'signature': ''
            })

        return symbols

    # ----- Incremental Indexing -----
    def _get_last_indexed_sha(self) -> Optional[str]:
        """Fetch the last indexed commit SHA from repository_metadata."""
        if not supabase_client:
            return None
        try:
            res = supabase_client.table('repository_metadata').select('last_indexed_sha').eq('name', self.repo_id).execute()
            if res.data and res.data[0].get('last_indexed_sha'):
                return res.data[0]['last_indexed_sha']
        except Exception as e:
            logger.warning(f"Could not fetch last_indexed_sha: {e}")
        return None

    def _update_last_indexed_sha(self):
        """Update the repository_metadata with the new commit SHA."""
        if not supabase_client:
            return
        try:
            supabase_client.table('repository_metadata').update({
                'last_indexed_sha': self.commit_sha,
                'updated_at': datetime.now(timezone.utc).isoformat()
            }).eq('name', self.repo_id).execute()
        except Exception as e:
            logger.warning(f"Could not update last_indexed_sha: {e}")

    def _should_skip_incremental(self) -> bool:
        """Check if we can skip re-indexing (same commit SHA already indexed)."""
        if self.commit_sha == 'HEAD':
            return False  # Always re-index when SHA is unknown
        last_sha = self._get_last_indexed_sha()
        if last_sha and last_sha == self.commit_sha:
            logger.info(f"Repository {self.repo_id} already indexed at {self.commit_sha}. Skipping.")
            return True
        return False

    # ----- Resumable Job State -----
    def _create_job(self, total_files: int) -> str:
        """Create a durable index_jobs record and return job_id."""
        job_id = str(uuid.uuid4())
        if supabase_client:
            try:
                supabase_client.table('index_jobs').insert({
                    'id': job_id,
                    'repository_id': self.repo_id,
                    'status': 'running',
                    'total_files': total_files,
                    'commit_sha': self.commit_sha,
                    'started_at': datetime.now(timezone.utc).isoformat()
                }).execute()
            except Exception as e:
                logger.warning(f"Could not create index_jobs record: {e}")
        return job_id

    def _get_processed_files(self, job_id: str) -> List[str]:
        """Fetch already-processed files from a previous job run for resumption."""
        if not supabase_client:
            return []
        try:
            res = supabase_client.table('index_jobs').select('processed_files').eq('id', job_id).execute()
            if res.data and res.data[0].get('processed_files'):
                return res.data[0]['processed_files']
        except Exception:
            pass
        return []

    def _update_job_progress(self, job_id: str, file_path: str, processed_files: List[str]):
        """Update durable job state after each file is processed."""
        if not supabase_client:
            return
        try:
            supabase_client.table('index_jobs').update({
                'progress': len(processed_files),
                'current_file': file_path,
                'last_successful_file': file_path,
                'processed_files': processed_files
            }).eq('id', job_id).execute()
        except Exception as e:
            logger.warning(f"Could not update job progress: {e}")

    def _complete_job(self, job_id: str, status: str = 'completed', error: str = None):
        """Mark job as completed or failed."""
        if not supabase_client:
            return
        try:
            update = {
                'status': status,
                'completed_at': datetime.now(timezone.utc).isoformat()
            }
            if error:
                update['error'] = error
            supabase_client.table('index_jobs').update(update).eq('id', job_id).execute()
        except Exception as e:
            logger.warning(f"Could not complete job: {e}")

    # ----- Persist Symbols to repository_symbols Table -----
    def _persist_symbols(self, symbols: List[Dict[str, Any]]):
        """Store extracted AST symbols in repository_symbols table."""
        if not supabase_client or not symbols:
            return
        try:
            records = []
            for sym in symbols:
                records.append({
                    'id': str(uuid.uuid4()),
                    'repository_id': self.repo_id,
                    'symbol_name': sym['symbol_name'],
                    'symbol_type': sym['symbol_type'],
                    'file_path': sym['file_path'],
                    'start_line': sym['start_line'],
                    'end_line': sym['end_line'],
                    'language': EXT_LANG_MAP.get(os.path.splitext(sym['file_path'])[1].lower(), 'unknown'),
                    'signature': sym.get('signature', '')
                })
            # Batch insert
            supabase_client.table('repository_symbols').insert(records).execute()
            logger.info(f"Persisted {len(records)} symbols to repository_symbols.")
        except Exception as e:
            logger.error(f"Error persisting symbols: {e}")

    # ----- Main Indexing Entrypoint -----
    def index_repository(self, job_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Indexes the repository: extracts AST symbols, computes embeddings,
        persists to Supabase, with incremental and resumable support.
        """
        # Incremental check
        if self._should_skip_incremental():
            return {
                'status': 'skipped',
                'repo_id': self.repo_id,
                'reason': f'Already indexed at {self.commit_sha}'
            }

        logger.info(f"Starting repository indexing for repo_id={self.repo_id}, commit={self.commit_sha}")

        # Collect all indexable files
        supported_extensions = ('.ts', '.tsx', '.js', '.jsx', '.py', '.java')
        all_file_paths = []

        for root, _, files in os.walk(self.repo_path):
            if any(ignored in root for ignored in ['node_modules', '.git', '.next', 'dist', 'build', '__pycache__']):
                continue
            for file in files:
                if file.endswith(supported_extensions):
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, self.repo_path).replace('\\', '/')
                    all_file_paths.append((full_path, rel_path))

        # Create durable job
        if not job_id:
            job_id = self._create_job(len(all_file_paths))

        # Resumable: skip already-processed files
        already_processed = set(self._get_processed_files(job_id))

        processed_files = list(already_processed)
        failed_files = []
        all_symbols = []
        all_edges = []

        for full_path, rel_path in all_file_paths:
            if rel_path in already_processed:
                logger.debug(f"Skipping already-processed file: {rel_path}")
                continue

            try:
                with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()

                symbols = self.parse_ast_symbols(rel_path, content)
                all_symbols.extend(symbols)
                
                edges = self._extract_edges(rel_path, content)
                all_edges.extend(edges)
                
                processed_files.append(rel_path)

                # Update job progress after each file
                self._update_job_progress(job_id, rel_path, processed_files)

            except Exception as e:
                logger.error(f"Failed to process file {rel_path}: {e}")
                failed_files.append({'file': rel_path, 'error': str(e)})

        # Persist AST symbols to repository_symbols table
        self._persist_symbols(all_symbols)
        
        # Persist extracted edges to repository_edges table
        if supabase_client and all_edges:
            try:
                # Batch insert in chunks of 500 to avoid size limits
                chunk_size = 500
                for i in range(0, len(all_edges), chunk_size):
                    supabase_client.table('repository_edges').insert(all_edges[i:i+chunk_size]).execute()
                logger.info(f"Persisted {len(all_edges)} edges to repository_edges.")
            except Exception as e:
                logger.error(f"Error persisting edges: {e}")

        # Compute embeddings for extracted symbols
        contents = [s['content'] for s in all_symbols]
        embeddings = generate_embeddings(contents) if contents else []

        # Store embeddings in code_embeddings table (BATCHED)
        if supabase_client and all_symbols and embeddings:
            try:
                records = []
                for sym, emb in zip(all_symbols, embeddings):
                    records.append({
                        'id': str(uuid.uuid4()),
                        'repository_id': self.repo_id,
                        'commit_sha': self.commit_sha,
                        'file_path': sym['file_path'],
                        'language': EXT_LANG_MAP.get(os.path.splitext(sym['file_path'])[1].lower(), 'unknown'),
                        'symbol_name': sym['symbol_name'],
                        'symbol_type': sym['symbol_type'],
                        'start_line': sym['start_line'],
                        'end_line': sym['end_line'],
                        'content': sym['content'],
                        'embedding': emb,
                        'created_at': datetime.now(timezone.utc).isoformat()
                    })
                # Batch insert in chunks of 100 to avoid size limits
                chunk_size = 100
                for i in range(0, len(records), chunk_size):
                    supabase_client.table('code_embeddings').insert(records[i:i+chunk_size]).execute()
                logger.info(f"Persisted {len(records)} embeddings to code_embeddings.")
            except Exception as e:
                logger.error(f"Error persisting embeddings to Supabase: {e}")

        # Update repository_metadata with last indexed SHA
        self._update_last_indexed_sha()

        # Complete the job
        self._complete_job(job_id)

        result = {
            'status': 'completed',
            'repo_id': self.repo_id,
            'commit_sha': self.commit_sha,
            'job_id': job_id,
            'total_files': len(processed_files),
            'total_symbols': len(all_symbols),
            'tree_sitter_used': _TREE_SITTER_AVAILABLE,
            'processed_files': processed_files,
            'failed_files': failed_files
        }

        logger.info(f"Indexing completed: {result['total_files']} files, {result['total_symbols']} symbols (Tree-sitter: {_TREE_SITTER_AVAILABLE})")
        return result
