import logging
from typing import List

logger = logging.getLogger(__name__)

_model = None

def get_embedding_model():
    global _model
    if _model is None:
        try:
            from fastembed import TextEmbedding
            # Using all-MiniLM-L6-v2 (384-dimensional vectors, 100% free & fast)
            _model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
            logger.info("FastEmbed model initialized successfully.")
        except Exception as e:
            logger.warning(f"FastEmbed initialization failed: {e}. Falling back to pseudo-embeddings for testing.")
            _model = "pseudo"
    return _model

def generate_embeddings(texts: List[str]) -> List[List[float]]:
    """Generates 384-dimensional vector embeddings for a list of strings."""
    model = get_embedding_model()
    if model == "pseudo" or model is None:
        # Fallback pseudo-embedding of 384 floats for testing without fastembed installed
        return [[0.01 * (i % 10) for i in range(384)] for _ in texts]
    
    embeddings = list(model.embed(texts))
    return [e.tolist() for e in embeddings]
