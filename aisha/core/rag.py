"""ChromaDB wrapper for semantic RAG over conversations and knowledge.

ChromaDB is a derived index — it can always be rebuilt from SQLite.
That's why ``memory/chroma/`` is gitignored; ``memory/dump.sql`` is not.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .config import CHROMA_DIR, settings

log = logging.getLogger(__name__)

_client = None
_collections: dict[str, Any] = {}


def _get_client():
    global _client
    if _client is not None:
        return _client
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    _client = chromadb.PersistentClient(
        path=str(CHROMA_DIR),
        settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
    )
    log.info("rag: chroma client initialised at %s", CHROMA_DIR)
    return _client


def _embedder():
    from chromadb.utils import embedding_functions
    return embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=settings.embedding_model,
    )


def collection(name: str):
    if name in _collections:
        return _collections[name]
    client = _get_client()
    col = client.get_or_create_collection(
        name=name,
        embedding_function=_embedder(),
    )
    _collections[name] = col
    return col


def index_conversation(row_id: int, content: str, metadata: Optional[dict] = None) -> None:
    """Upsert a conversation turn into the ``conversations`` collection."""
    if not content or not content.strip():
        return
    col = collection("conversations")
    col.upsert(
        ids=[f"conv-{row_id}"],
        documents=[content],
        metadatas=[metadata or {}],
    )


def index_pair(
    user_row: int,
    user_text: str,
    asst_row: int,
    asst_text: str,
    metadata: Optional[dict] = None,
) -> None:
    """Upsert a paired user+assistant exchange as a single document.

    Pair embeddings let retrieval match on the *joint* content of an exchange.
    A user question like "why the Stripe → Adyen move?" often doesn't embed
    near an assistant answer that never restates the question; embedding the
    pair together bridges that gap. Both the per-turn and the paired doc are
    kept — search merges and dedupes them via RRF in ``chat._semantic_hint``.
    """
    if not user_text or not user_text.strip():
        return
    if not asst_text or not asst_text.strip():
        return
    col = collection("conversations")
    doc = f"[user] {user_text}\n[aisha] {asst_text}"
    meta = dict(metadata or {})
    meta.update({"kind": "pair", "user_row": user_row, "asst_row": asst_row})
    col.upsert(
        ids=[f"pair-{user_row}-{asst_row}"],
        documents=[doc],
        metadatas=[meta],
    )


def search_conversations(query: str, *, limit: int = 10) -> list[dict]:
    col = collection("conversations")
    result = col.query(query_texts=[query], n_results=limit)
    docs = result.get("documents", [[]])[0]
    metas = result.get("metadatas", [[]])[0]
    ids = result.get("ids", [[]])[0]
    distances = result.get("distances", [[]])[0]
    return [
        {"id": i, "content": d, "metadata": m, "distance": dist}
        for i, d, m, dist in zip(ids, docs, metas, distances)
    ]


def reset() -> None:
    """Drop all collections — used before a full reindex."""
    client = _get_client()
    client.reset()
    _collections.clear()
    log.info("rag: chroma reset")
