"""
RAG-RBAC Proof of Concept — FastAPI Application

Endpoints:
  POST /api/auth/login        → Get JWT token
  POST /api/auth/register     → Register (admin only)
  GET  /api/users             → List users (admin only)
  DELETE /api/users/{id}      → Delete user (admin only)
  PUT  /api/users/{id}/role   → Change role (admin only)
  POST /api/documents/upload  → Upload & ingest document
  GET  /api/documents         → List user's documents
  DELETE /api/documents/{id}  → Delete document + vectors
  POST /api/query             → RAG query with RBAC
  GET  /api/stats             → System stats (admin only)
  GET  /                      → Dashboard UI
"""
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.database import (
    init_db, create_user, authenticate, list_users,
    delete_user, update_user_role, record_document,
    list_documents, delete_document,
)
from app.auth import create_token, get_current_user, require_admin
from app.rag import init_collection, ingest_document, query_rag, delete_user_vectors, get_collection_stats


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print("Initializing database...")
    init_db()
    print("Initializing Qdrant collection...")
    init_collection()
    print("✓ RAG-RBAC system ready")
    yield
    # Shutdown (cleanup if needed)


app = FastAPI(
    title="RAG-RBAC PoC",
    description="RAG with Role-Based Access Control at the embedding level",
    version="0.1.0",
    lifespan=lifespan,
)


# ── Pydantic Models ──

class LoginRequest(BaseModel):
    username: str
    password: str

class RegisterRequest(BaseModel):
    username: str
    password: str
    role: str = "user"

class RoleUpdate(BaseModel):
    role: str

class QueryRequest(BaseModel):
    question: str
    top_k: int = 5


# ── Auth Routes ──

@app.post("/api/auth/login")
async def login(req: LoginRequest):
    user = authenticate(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token(user["id"], user["username"], user["role"])
    return {"token": token, "user": user}


@app.post("/api/auth/register")
async def register(req: RegisterRequest, admin: dict = Depends(require_admin)):
    if req.role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="Role must be 'user' or 'admin'")
    user = create_user(req.username, req.password, req.role)
    if not user:
        raise HTTPException(status_code=409, detail="Username already exists")
    return {"message": f"User '{req.username}' created", "user": user}


# ── User Management (Admin) ──

@app.get("/api/users")
async def get_users(admin: dict = Depends(require_admin)):
    return {"users": list_users()}


@app.delete("/api/users/{user_id}")
async def remove_user(user_id: int, admin: dict = Depends(require_admin)):
    # Also delete their vectors
    delete_user_vectors(user_id)
    if not delete_user(user_id):
        raise HTTPException(status_code=404, detail="User not found or is admin")
    return {"message": "User and all associated data deleted"}


@app.put("/api/users/{user_id}/role")
async def change_role(user_id: int, req: RoleUpdate, admin: dict = Depends(require_admin)):
    if req.role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="Role must be 'user' or 'admin'")
    if not update_user_role(user_id, req.role):
        raise HTTPException(status_code=404, detail="User not found")
    return {"message": f"Role updated to '{req.role}'"}


# ── Document Routes ──

@app.post("/api/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    # Validate file type
    allowed = {"application/pdf", "text/plain", "text/markdown", "text/csv"}
    content_type = file.content_type or ""
    filename = file.filename or "unknown"

    if content_type not in allowed and not filename.endswith((".txt", ".md", ".pdf", ".csv")):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {content_type}. Allowed: PDF, TXT, MD, CSV",
        )

    content = await file.read()
    if len(content) > 20 * 1024 * 1024:  # 20MB limit
        raise HTTPException(status_code=400, detail="File too large (max 20MB)")

    # Determine content type for processing
    if filename.endswith(".pdf"):
        content_type = "application/pdf"
    else:
        content_type = "text/plain"

    try:
        chunk_count = await ingest_document(
            user_id=user["user_id"],
            filename=filename,
            content=content,
            content_type=content_type,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Record in SQLite
    doc_id = record_document(user["user_id"], filename, chunk_count)

    return {
        "message": f"Ingested '{filename}' → {chunk_count} chunks",
        "document_id": doc_id,
        "chunk_count": chunk_count,
    }


@app.get("/api/documents")
async def get_documents(user: dict = Depends(get_current_user)):
    if user["role"] == "admin":
        docs = list_documents()  # Admin sees all
    else:
        docs = list_documents(user["user_id"])
    return {"documents": docs}


@app.delete("/api/documents/{doc_id}")
async def remove_document(doc_id: int, user: dict = Depends(get_current_user)):
    if user["role"] == "admin":
        filename = delete_document(doc_id)
    else:
        filename = delete_document(doc_id, user["user_id"])

    if not filename:
        raise HTTPException(status_code=404, detail="Document not found")

    # Delete associated vectors from Qdrant
    delete_user_vectors(user["user_id"], filename)
    return {"message": f"Document '{filename}' and its vectors deleted"}


# ── RAG Query ──

@app.post("/api/query")
async def rag_query(req: QueryRequest, user: dict = Depends(get_current_user)):
    result = await query_rag(
        question=req.question,
        user_id=user["user_id"],
        role=user["role"],
        top_k=req.top_k,
    )
    return result


# ── Stats (Admin) ──

@app.get("/api/stats")
async def system_stats(admin: dict = Depends(require_admin)):
    users = list_users()
    docs = list_documents()
    vec_stats = get_collection_stats()
    return {
        "total_users": len(users),
        "total_documents": len(docs),
        "total_vectors": vec_stats["total_vectors"],
        "qdrant_status": vec_stats["status"],
    }


# ── Health Check ──

@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "rag-rbac"}


# ── Serve Dashboard UI ──

@app.get("/")
async def serve_dashboard():
    return FileResponse("app/static/index.html")
