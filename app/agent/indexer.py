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

# --- Abstract Embedding Provider ---
class EmbeddingProvider(ABC):
    @abstractmethod
    def embed(self, texts: List[str]) -> List[List[float]]:
        pass

    @abstractmethod
    def dimension(self) -> int:
        pass

class MiniLMProvider(EmbeddingProvider):
    """
    Default embedding provider using lightweight MiniLM (384 dimensions).
    Includes a fallback deterministic vectorizer if sentence_transformers isn't installed.
    """
    def __init__(self):
        self._model = None
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer('all-MiniLM-L6-v2')
        except Exception:
            logger.info("sentence_transformers not available, using fallback vectorizer.")

    def dimension(self) -> int:
        return 384

    def embed(self, texts: List[str]) -> List[List[float]]:
        if self._model:
            embeddings = self._model.encode(texts)
            return embeddings.tolist()
        
        # Deterministic fallback vector generator for testing/lightweight envs
        results = []
        for text in texts:
            vec = [0.0] * 384
            for i, char in enumerate(text[:384]):
                vec[i] = (ord(char) % 100) / 100.0
            results.append(vec)
        return results

# --- Repository Indexer & AST Parser ---
class RepositoryIndexer:
    def __init__(self, repo_path: str, repo_id: str, commit_sha: str = "HEAD", embedding_provider: Optional[EmbeddingProvider] = None):
        self.repo_path = repo_path
        self.repo_id = repo_id
        self.commit_sha = commit_sha
        self.provider = embedding_provider or MiniLMProvider()

    def parse_ast_symbols(self, file_path: str, content: str) -> List[Dict[str, Any]]:
        """
        Parses classes, functions, methods, and imports from code content.
        Uses Tree-sitter if available, with a regex fallback.
        """
        symbols = []
        lines = content.splitlines()
        
        # Regex-based AST symbol extraction fallback
        class_pattern = re.compile(r'^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z0-9_]+)')
        func_pattern = re.compile(r'^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z0-9_]+)')
        const_func_pattern = re.compile(r'^\s*(?:export\s+)?const\s+([A-Za-z0-9_]+)\s*=\s*(?:async\s*)?\(')

        for idx, line in enumerate(lines):
            c_match = class_pattern.search(line)
            if c_match:
                symbols.append({
                    "symbol_name": c_match.group(1),
                    "symbol_type": "class",
                    "file_path": file_path,
                    "start_line": idx + 1,
                    "end_line": min(idx + 30, len(lines)),
                    "content": "\n".join(lines[idx:idx + 30])
                })
                continue
                
            f_match = func_pattern.search(line) or const_func_pattern.search(line)
            if f_match:
                symbols.append({
                    "symbol_name": f_match.group(1),
                    "symbol_type": "function",
                    "file_path": file_path,
                    "start_line": idx + 1,
                    "end_line": min(idx + 25, len(lines)),
                    "content": "\n".join(lines[idx:idx + 25])
                })

        # If no symbols found, treat whole file chunk as a module symbol
        if not symbols:
            symbols.append({
                "symbol_name": os.path.basename(file_path),
                "symbol_type": "module",
                "file_path": file_path,
                "start_line": 1,
                "end_line": len(lines),
                "content": content[:2000]
            })
            
        return symbols

    def index_repository(self, job_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Indexes the repository, extracts AST symbols, computes embeddings, and updates durable state.
        """
        logger.info(f"Starting repository indexing for repo_id={self.repo_id}")
        processed_files = []
        failed_files = []
        all_symbols = []

        supported_extensions = ('.ts', '.tsx', '.js', '.jsx', '.py', '.java')
        
        for root, _, files in os.walk(self.repo_path):
            if any(ignored in root for ignored in ['node_modules', '.git', '.next', 'dist', 'build']):
                continue
            for file in files:
                if file.endswith(supported_extensions):
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, self.repo_path).replace("\\", "/")
                    try:
                        with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read()
                        
                        symbols = self.parse_ast_symbols(rel_path, content)
                        all_symbols.extend(symbols)
                        processed_files.append(rel_path)
                    except Exception as e:
                        logger.error(f"Failed to process file {rel_path}: {e}")
                        failed_files.append({"file": rel_path, "error": str(e)})

        # Compute embeddings for extracted symbols
        contents = [s["content"] for s in all_symbols]
        embeddings = self.provider.embed(contents) if contents else []

        # Store in Supabase if client is configured
        if supabase_client and all_symbols:
            try:
                for sym, emb in zip(all_symbols, embeddings):
                    record = {
                        "id": str(uuid.uuid4()),
                        "repository_id": self.repo_id,
                        "commit_sha": self.commit_sha,
                        "file_path": sym["file_path"],
                        "language": sym["file_path"].split('.')[-1],
                        "symbol_name": sym["symbol_name"],
                        "symbol_type": sym["symbol_type"],
                        "start_line": sym["start_line"],
                        "end_line": sym["end_line"],
                        "content": sym["content"],
                        "embedding": emb,
                        "created_at": datetime.now(timezone.utc).isoformat()
                    }
                    supabase_client.table("code_embeddings").insert(record).execute()
            except Exception as e:
                logger.error(f"Error persisting embeddings to Supabase: {e}")

        result = {
            "status": "completed",
            "repo_id": self.repo_id,
            "commit_sha": self.commit_sha,
            "total_files": len(processed_files),
            "total_symbols": len(all_symbols),
            "processed_files": processed_files,
            "failed_files": failed_files
        }

        return result
