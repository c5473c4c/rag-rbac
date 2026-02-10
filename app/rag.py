"""
RAG pipeline with per-user RBAC enforced at the Qdrant embedding level.

Every document chunk is stored with metadata:
  - user_id: owner of the document
  - filename: source file
  - chunk_index: position in document

Query-time filtering:
  - Regular users: filter to user_id == current_user
  - Admins: no filter (access all embeddings)
"""
import os
import re
import uuid
import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
    PayloadSchemaType,
)

OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
QDRANT_HOST = os.getenv("QDRANT_HOST", "qdrant")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
LLM_MODEL = os.getenv("LLM_MODEL", "nemotron-mini")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")

COLLECTION = "documents"
EMBED_DIM = 768  # nomic-embed-text dimension
CHUNK_SIZE = 500  # characters per chunk
CHUNK_OVERLAP = 50
TOP_K = 5

qdrant: QdrantClient | None = None


def get_qdrant() -> QdrantClient:
    global qdrant
    if qdrant is None:
        qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    return qdrant


def init_collection():
    """Create the Qdrant collection with payload indexes for RBAC filtering."""
    client = get_qdrant()
    collections = [c.name for c in client.get_collections().collections]
    if COLLECTION not in collections:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
        )
        # Index user_id for fast RBAC filtering
        client.create_payload_index(
            collection_name=COLLECTION,
            field_name="user_id",
            field_schema=PayloadSchemaType.INTEGER,
        )
        print(f"✓ Created Qdrant collection '{COLLECTION}' with user_id index")
    else:
        print(f"✓ Qdrant collection '{COLLECTION}' already exists")


# ── Text Processing ──

def chunk_text(text: str) -> list[str]:
    """Split text into overlapping chunks."""
    text = re.sub(r'\s+', ' ', text).strip()
    chunks = []
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk.strip())
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def extract_text_from_pdf(content: bytes) -> str:
    """Extract text from a PDF file."""
    from PyPDF2 import PdfReader
    import io
    reader = PdfReader(io.BytesIO(content))
    pages = []
    for page in reader.pages:
        t = page.extract_text()
        if t:
            pages.append(t)
    return "\n".join(pages)


# ── Ollama Integration ──

async def get_embedding(text: str) -> list[float]:
    """Get embedding vector from Ollama."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": EMBED_MODEL, "input": text},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["embeddings"][0]


async def llm_generate(prompt: str, system: str = "") -> str:
    """Generate text from the local LLM."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": LLM_MODEL,
                "prompt": prompt,
                "system": system,
                "stream": False,
                "options": {"temperature": 0.3, "num_predict": 1024},
            },
        )
        resp.raise_for_status()
        return resp.json()["response"]


# ── Ingest (with RBAC metadata) ──

async def ingest_document(
    user_id: int, filename: str, content: bytes, content_type: str
) -> int:
    """
    Ingest a document: extract text, chunk, embed, store with user_id metadata.
    Returns the number of chunks created.
    """
    # Extract text
    if content_type == "application/pdf":
        text = extract_text_from_pdf(content)
    else:
        text = content.decode("utf-8", errors="replace")

    if not text.strip():
        raise ValueError("No text could be extracted from the document")

    chunks = chunk_text(text)
    if not chunks:
        raise ValueError("Document produced no chunks after processing")

    # Embed all chunks
    points = []
    for i, chunk in enumerate(chunks):
        vector = await get_embedding(chunk)
        points.append(
            PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={
                    "user_id": user_id,       # ← RBAC key
                    "filename": filename,
                    "chunk_index": i,
                    "text": chunk,
                },
            )
        )

    # Upsert to Qdrant
    client = get_qdrant()
    # Batch upsert in groups of 100
    batch_size = 100
    for i in range(0, len(points), batch_size):
        client.upsert(collection_name=COLLECTION, points=points[i : i + batch_size])

    return len(chunks)


# ── Query (with RBAC enforcement) ──

async def query_rag(
    question: str, user_id: int, role: str, top_k: int = TOP_K
) -> dict:
    """
    RAG query with RBAC filtering.

    - user role  → only searches embeddings where user_id matches
    - admin role → searches ALL embeddings (no filter)
    """
    query_vector = await get_embedding(question)

    # ┌──────────────────────────────────────────┐
    # │  RBAC FILTER — the core access control   │
    # └──────────────────────────────────────────┘
    if role == "admin":
        query_filter = None  # Admin sees everything
    else:
        query_filter = Filter(
            must=[
                FieldCondition(
                    key="user_id",
                    match=MatchValue(value=user_id),
                )
            ]
        )

    client = get_qdrant()
    results = client.query_points(
        collection_name=COLLECTION,
        query=query_vector,
        query_filter=query_filter,
        limit=top_k,
        with_payload=True,
    )

    if not results.points:
        return {
            "answer": "No relevant documents found in your accessible knowledge base.",
            "sources": [],
            "chunks_searched": 0,
        }

    # Build context from retrieved chunks
    context_parts = []
    sources = []
    for point in results.points:
        payload = point.payload
        context_parts.append(payload["text"])
        sources.append({
            "filename": payload["filename"],
            "chunk_index": payload["chunk_index"],
            "owner_id": payload["user_id"],
            "score": round(point.score, 4),
        })

    context = "\n\n---\n\n".join(context_parts)

    # Generate answer with LLM
    system_prompt = (
        "You are a helpful assistant that answers questions based on the provided context. "
        "Only use information from the context below. If the context doesn't contain "
        "enough information to answer, say so clearly. Cite which source documents "
        "you drew from."
    )
    prompt = f"""Context (retrieved documents):
{context}

Question: {question}

Answer based on the context above:"""

    answer = await llm_generate(prompt, system=system_prompt)

    return {
        "answer": answer,
        "sources": sources,
        "chunks_searched": len(results.points),
    }


# ── Deletion (RBAC-aware) ──

def delete_user_vectors(user_id: int, filename: str | None = None):
    """Delete vectors for a user, optionally filtered by filename."""
    client = get_qdrant()
    conditions = [FieldCondition(key="user_id", match=MatchValue(value=user_id))]
    if filename:
        conditions.append(
            FieldCondition(key="filename", match=MatchValue(value=filename))
        )
    client.delete(
        collection_name=COLLECTION,
        points_selector=Filter(must=conditions),
    )


def get_collection_stats() -> dict:
    """Get collection statistics for admin dashboard."""
    client = get_qdrant()
    try:
        info = client.get_collection(COLLECTION)
        return {
            "total_vectors": info.points_count,
            "status": info.status.value,
        }
    except Exception:
        return {"total_vectors": 0, "status": "unknown"}
