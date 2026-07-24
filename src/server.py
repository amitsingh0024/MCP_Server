import os
import uuid
import logging
import threading
import shutil
from pathlib import Path
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

import src.db as db
import src.queue as queue
from src.config import get_config
from src.mcp_server import mcp

# Set up logs
logger = logging.getLogger("pdfspecs.server")

app = FastAPI(title="OpenPDFSpecs API & MCP Server")

# Custom Authentication Middleware
class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
            
        path = request.url.path
        method = request.method
        
        # Check if auth is required (disabled if no API keys exist, enabling first-run setup)
        db.init_db()
        auth_required = len(db.list_api_keys()) > 0
        
        is_mcp = path.startswith("/mcp")
        is_api = path.startswith("/api/v1")
        is_public = path == "/api/v1/config/provider" or (not auth_required)
        
        if (is_mcp or is_api) and not is_public:
            # Check Authorization Header
            auth_header = request.headers.get("Authorization")
            token = None
            if auth_header and auth_header.startswith("Bearer "):
                token = auth_header.replace("Bearer ", "")
            else:
                # Check Query Parameter (useful for SSE connections)
                token = request.query_params.get("token")
                
            if not token or not db.verify_api_key(token):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Unauthorized: Invalid or missing OpenPDFSpecs API key"}
                )
                
        return await call_next(request)

app.add_middleware(AuthMiddleware)

# Configure CORS so Next.js on port 3000 can talk to FastAPI on port 8000
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # In development, allow all origins
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount the FastMCP SSE App under /mcp
app.mount("/mcp", mcp.sse_app())

@app.on_event("startup")
def startup_event():
    # 1. Initialize DB
    db.init_db()
    # 2. Reset stuck tasks from previous crash
    queue.reset_stuck_tasks()
    # 3. Start background worker thread
    logger.info("Starting background ingestion worker thread...")
    worker_thread = threading.Thread(target=queue.run_worker_loop, daemon=True)
    worker_thread.start()

# REST Endpoints
@app.get("/api/v1/config/provider")
def get_public_provider_info():
    """Returns provider status (needed by frontend to verify if setup is done)."""
    db.init_db()
    cfg = get_config()
    # Re-load config from database
    cfg.load()
    
    gemini_key = db.get_setting("gemini_api_key", decrypt=True)
    nvidia_key = db.get_setting("nvidia_api_key", decrypt=True)
    auth_required = len(db.list_api_keys()) > 0
    
    return {
        "provider": cfg.provider,
        "has_gemini_key": bool(gemini_key),
        "has_nvidia_key": bool(nvidia_key),
        "llm_model": cfg.llm_model,
        "embedding_model": cfg.embedding_model,
        "chunk_size": cfg.chunk_size,
        "chunk_overlap": cfg.chunk_overlap,
        "auth_required": auth_required
    }

@app.post("/api/v1/config")
def save_system_config(payload: dict):
    """Saves settings in the database (keys encrypted) and reloads config."""
    db.init_db()
    
    provider = payload.get("provider")
    if provider and provider in ["gemini", "nvidia"]:
        db.save_setting("provider", provider)
        
    if "gemini_api_key" in payload:
        db.save_setting("gemini_api_key", payload["gemini_api_key"], encrypt=True)
        
    if "nvidia_api_key" in payload:
        db.save_setting("nvidia_api_key", payload["nvidia_api_key"], encrypt=True)
        
    if "llm_model" in payload:
        db.save_setting("llm_model", payload["llm_model"])
        
    if "embedding_model" in payload:
        db.save_setting("embedding_model", payload["embedding_model"])
        
    if "chunk_size" in payload:
        db.save_setting("chunk_size", str(int(payload["chunk_size"])))
        
    if "chunk_overlap" in payload:
        db.save_setting("chunk_overlap", str(int(payload["chunk_overlap"])))
        
    # Reload local config instance
    get_config().load()
    return {"status": "success", "message": "Settings updated successfully."}

@app.get("/api/v1/auth/tokens")
def get_auth_tokens():
    """Lists all active agent tokens."""
    return db.list_api_keys()

@app.post("/api/v1/auth/tokens")
def generate_auth_token(payload: dict):
    """Generates a new secure sk-pdfspecs-... API key."""
    desc = payload.get("description", "Agent Access Key")
    token = f"sk-pdfspecs-{uuid.uuid4()}"
    db.add_api_key(token, desc)
    return {"token": token, "description": desc}

@app.delete("/api/v1/auth/tokens/{token}")
def revoke_auth_token(token: str):
    """Revokes an agent API key."""
    db.delete_api_key(token)
    return {"status": "success", "message": "API key revoked successfully."}

@app.get("/api/v1/documents")
def get_documents():
    """Lists all cataloged documents."""
    return db.list_documents()

@app.delete("/api/v1/documents/{doc_id}")
def delete_document(doc_id: str):
    """Deletes an ingested document."""
    db.delete_document(doc_id)
    return {"status": "success", "message": "Document deleted successfully."}

@app.get("/api/v1/tasks")
def get_tasks():
    """Lists task queue statuses."""
    return queue.list_tasks()

@app.post("/api/v1/ingest")
async def ingest_document(file: UploadFile = File(...)):
    """Handles PDF upload and queues it for async background ingestion."""
    # Ensure upload directory exists
    upload_dir = Path.home() / ".pdfspecs" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    
    # Save the file locally
    temp_path = upload_dir / f"{uuid.uuid4()}-{file.filename}"
    try:
        with temp_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save uploaded file: {e}")
        
    # Add to SQLite task queue
    try:
        task_id = queue.add_task(str(temp_path))
        return {
            "status": "success",
            "message": "File uploaded and queued for processing.",
            "task_id": task_id,
            "filename": file.filename
        }
    except Exception as e:
        # Clean up temp file on failure
        if temp_path.exists():
            os.remove(temp_path)
        raise HTTPException(status_code=500, detail=f"Failed to queue ingestion task: {e}")

@app.get("/api/v1/metrics")
def get_metrics():
    """Returns token usage metrics for ingestion and sandbox."""
    db.init_db()
    return db.get_token_metrics()

@app.post("/api/v1/search")
def search_documents(payload: dict):
    """Exposes direct RAG search for external agents/LLMs, secured by API key."""
    query = payload.get("query")
    if not query:
        raise HTTPException(status_code=400, detail="Missing 'query' in request payload.")
    limit = payload.get("limit", 5)
    
    from src.mcp_server import retrieve_chunks_for_query
    import src.enricher as enricher
    try:
        chunks = retrieve_chunks_for_query(query, limit)
        if not chunks:
            raw_results = f"No matches found for search query: '{query}'."
            synthesis = "No context found to synthesize an assessment."
        else:
            # Construct the markdown references list
            output_parts = [f"# Search Results for: '{query}'", ""]
            for idx, row in enumerate(chunks):
                score = round(row["score"], 3)
                output_parts.append(f"### {idx+1}. {row['filename']} (Pages {row['page_start']}-{row['page_end']})")
                output_parts.append(f"- **Chunk ID**: `{row['id']}`")
                output_parts.append(f"- **Relevance Score**: `{score}`")
                output_parts.append(f"- **Summary**: {row['summary']}")
                snippet = row['text_content'][:400].replace('\n', ' ')
                output_parts.append(f"- **Text Snippet**: \"{snippet}...\"")
                output_parts.append("")
            raw_results = "\n".join(output_parts)
            
            # Generate RAG response (synthesis)
            synthesis = enricher.generate_rag_response(query, chunks)
            
        return {
            "results": raw_results,
            "synthesis": synthesis
        }
    except Exception as e:
        logger.exception(f"RAG search error: {e}")
        raise HTTPException(status_code=500, detail=f"Search failed: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.server:app", host="0.0.0.0", port=8000, reload=True, reload_dirs=["src"])



