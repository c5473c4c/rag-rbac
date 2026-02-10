# RAG-RBAC Proof of Concept

A fully containerized, locally-running RAG (Retrieval Augmented Generation) system with Role-Based Access Control enforced at the embedding level.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                     Docker Compose Stack                      │
│                                                              │
│  ┌─────────────┐   ┌─────────────┐   ┌──────────────────┐  │
│  │   Ollama     │   │   Qdrant    │   │   FastAPI App     │  │
│  │             │   │             │   │                  │  │
│  │ nemotron-   │   │  Vectors +  │   │  ┌─ Auth (JWT) │  │
│  │   mini      │◄──│  Metadata   │◄──│  ├─ RAG Engine │  │
│  │ nomic-embed │   │  Filtering  │   │  ├─ RBAC Logic │  │
│  │   -text     │   │             │   │  └─ Dashboard  │  │
│  └─────────────┘   └─────────────┘   └──────────────────┘  │
│       :11434            :6333              :8000              │
└──────────────────────────────────────────────────────────────┘
```

## How RBAC Works at the Embedding Level

This is the core concept of the system:

### Ingest (Write Path)
```
User uploads PDF  →  Extract text  →  Chunk  →  Embed via nomic-embed-text
                                                        │
                                                        ▼
                                              Store in Qdrant with:
                                              {
                                                vector: [0.12, -0.34, ...],
                                                payload: {
                                                  user_id: 7,        ← RBAC tag
                                                  filename: "q3.pdf",
                                                  text: "chunk text..."
                                                }
                                              }
```

### Query (Read Path)
```
User asks question  →  Embed question  →  Search Qdrant
                                                │
                                    ┌───────────┴───────────┐
                                    │                       │
                              role == "user"          role == "admin"
                                    │                       │
                              Filter: WHERE            No filter:
                              user_id = current        search ALL
                                    │                   vectors
                                    ▼                       ▼
                              Only MY chunks          ALL chunks
                                    │                       │
                                    └───────────┬───────────┘
                                                │
                                                ▼
                                    Build context → LLM generates answer
```

The RBAC filter is a **Qdrant payload filter** applied at query time. It's not application-level filtering after retrieval — Qdrant only returns matching vectors, so unauthorized data never leaves the database.

## Quick Start

### Prerequisites
- Docker & Docker Compose
- NVIDIA GPU + drivers (recommended) or CPU-only (slower)

### Launch

```bash
# Clone or copy this directory, then:
docker compose up --build

# First run pulls ~4GB of models — be patient
# App will be ready when you see:
#   ✓ RAG-RBAC system ready
```

### Access

Open **http://localhost:8000** in your browser.

Default admin credentials:
- Username: `admin`
- Password: `admin123`

### CPU-Only Mode

If you don't have an NVIDIA GPU, edit `docker-compose.yml`:
1. Remove the entire `deploy:` block under `ollama`
2. Uncomment the `environment:` block below it

## Usage Walkthrough

1. **Login** as `admin` / `admin123`
2. **Create users** via the Users page (e.g., `alice` and `bob`)
3. **Upload documents** — each user uploads their own PDFs/text files
4. **Query** — each user's RAG queries only see their own documents
5. **Admin queries** — the admin can query across ALL users' documents

### Testing RBAC

1. As `admin`, create two users: `alice` and `bob`
2. Login as `alice`, upload a document about Topic A
3. Login as `bob`, upload a document about Topic B
4. As `bob`, ask about Topic A → **no results** (RBAC blocks it)
5. As `admin`, ask about Topic A → **results found** (admin bypass)

## API Endpoints

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/auth/login` | None | Get JWT token |
| POST | `/api/auth/register` | Admin | Create user |
| GET | `/api/users` | Admin | List all users |
| DELETE | `/api/users/{id}` | Admin | Delete user + data |
| PUT | `/api/users/{id}/role` | Admin | Change role |
| POST | `/api/documents/upload` | User | Upload & ingest |
| GET | `/api/documents` | User | List documents |
| DELETE | `/api/documents/{id}` | User | Delete doc + vectors |
| POST | `/api/query` | User | RAG query (RBAC enforced) |
| GET | `/api/stats` | Admin | System statistics |

## Configuration

Environment variables in `docker-compose.yml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_MODEL` | `nemotron-mini` | Ollama LLM model |
| `EMBED_MODEL` | `nomic-embed-text` | Embedding model |
| `JWT_SECRET` | (set in compose) | Token signing key |
| `DEFAULT_ADMIN_PASSWORD` | `admin123` | Initial admin password |

### Swapping Models

To use a different LLM (e.g., `llama3.2`, `mistral`, `phi3`), just change `LLM_MODEL` in the compose file. The entrypoint script will auto-pull it on next boot.

## Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| LLM | Ollama + nemotron-mini | Local inference |
| Embeddings | nomic-embed-text (768d) | Vectorization |
| Vector DB | Qdrant | Storage + RBAC filtering |
| Backend | FastAPI + Python 3.11 | API + RAG pipeline |
| Auth | JWT + bcrypt | User sessions |
| User Store | SQLite | User/document metadata |
| Frontend | Vanilla HTML/CSS/JS | Dashboard UI |
| Orchestration | Docker Compose | Container management |

## Production Considerations

This is a PoC. For production, you'd want:

- **Persistent JWT secret** (not hardcoded)
- **PostgreSQL** instead of SQLite
- **TLS termination** (nginx/caddy in front)
- **Rate limiting** on query endpoints
- **Document versioning** and chunk deduplication
- **Async ingestion queue** (Celery/Redis) for large uploads
- **Proper logging** (structured JSON, log aggregation)
- **Embedding cache** to avoid re-embedding duplicate content
- **Group-level RBAC** (teams, departments) in addition to user-level
