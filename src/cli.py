import sys
import argparse
import logging
from pathlib import Path
import numpy as np

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("pdfspecs.cli")

import src.db as db
import src.queue as queue
import src.config as config
import src.enricher as enricher

def handle_init(args):
    """Initializes the database."""
    logger.info("Initializing database...")
    db.init_db()
    logger.info("Database initialized successfully.")

def handle_ingest(args):
    """Ingests a PDF by adding it to the queue and executing it immediately."""
    db.init_db()
    file_path = Path(args.file_path).resolve()
    if not file_path.exists():
        logger.error(f"Error: PDF file does not exist at: {args.file_path}")
        sys.exit(1)
        
    logger.info(f"Adding {file_path.name} to the queue...")
    task_id = queue.add_task(str(file_path))
    logger.info(f"Task created with ID: {task_id}. Processing immediately...")
    
    # Process it synchronously
    with db.get_connection() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    
    success = queue.process_single_task(dict(task))
    if success:
        logger.info("Ingestion completed successfully!")
    else:
        logger.error("Ingestion failed. Run 'python3 src/cli.py status' to see details.")
        sys.exit(1)

def handle_worker(args):
    """Starts the background worker loop to poll for pending files."""
    db.init_db()
    logger.info("Starting background ingestion worker. Press Ctrl+C to stop.")
    try:
        queue.run_worker_loop()
    except KeyboardInterrupt:
        logger.info("Worker stopped by user.")

def handle_ls(args):
    """Lists all ingested documents."""
    docs = db.list_documents()
    if not docs:
        print("No documents found. Use 'python3 src/cli.py ingest <pdf_path>' to add some.")
        return
        
    print(f"\n{'ID':<38} | {'Filename':<30} | {'Size (KB)':<10} | {'Created At':<20}")
    print("-" * 108)
    for doc in docs:
        size_kb = round(doc["size_bytes"] / 1024, 1)
        print(f"{doc['id']:<38} | {doc['filename'][:30]:<30} | {size_kb:<10} | {doc['created_at']:<20}")
    print()

def handle_status(args):
    """Shows the ingestion queue status."""
    tasks = queue.list_tasks()
    if not tasks:
        print("No tasks found in the ingestion queue.")
        return
        
    print(f"\n{'ID':<5} | {'Filename':<30} | {'Status':<12} | {'Attempts':<8} | {'Error'}")
    print("-" * 100)
    for t in tasks:
        fname = Path(t["file_path"]).name
        err = t["error_message"] if t["error_message"] else ""
        print(f"{t['id']:<5} | {fname[:30]:<30} | {t['status']:<12} | {t['attempts']:<8} | {err[:40]}")
    print()

def handle_set_provider(args):
    """Sets the API provider."""
    cfg = config.get_config()
    provider = args.provider.lower()
    if provider not in ["gemini", "nvidia"]:
        logger.error("Error: Provider must be 'gemini' or 'nvidia'.")
        sys.exit(1)
    cfg.provider = provider
    
    # Set matching defaults
    if provider == "gemini":
        cfg.llm_model = "gemini-1.5-flash"
        cfg.embedding_model = "text-embedding-004"
    else:
        cfg.llm_model = "meta/llama-3.1-8b-instruct"
        cfg.embedding_model = "nvidia/nv-embedqa-e5-v5"

        
    cfg.save()
    logger.info(f"API Provider set to '{provider}' with default models.")

def handle_set_key(args):
    """Sets the API key for the active provider."""
    cfg = config.get_config()
    if cfg.provider == "gemini":
        cfg.gemini_api_key = args.api_key
    elif cfg.provider == "nvidia":
        cfg.nvidia_api_key = args.api_key
    cfg.save()
    logger.info(f"API key successfully updated for '{cfg.provider}'.")

def handle_search(args):
    """Performs a test search across all ingested documents."""
    db.init_db()
    query = args.query
    limit = args.limit
    
    print(f"\nSearching for: '{query}'")
    
    # 1. Lexical search (FTS5)
    lexical_hits = db.query_lexical_fts(query, limit=limit * 2)
    lexical_map = {hit["chunk_id"]: hit for hit in lexical_hits}
    
    # 2. Semantic vector search (Cosine Similarity via NumPy)
    cfg = config.get_config()
    semantic_hits = []
    
    # Only run semantic search if key is configured
    has_key = (cfg.provider == "gemini" and cfg.gemini_api_key) or (cfg.provider == "nvidia" and cfg.nvidia_api_key)
    
    if has_key:
        try:
            # Embed query
            query_vector = None
            if cfg.provider == "gemini":
                query_vector = enricher._call_gemini_embedding(query, cfg.gemini_api_key, cfg.embedding_model)
            elif cfg.provider == "nvidia":
                query_vector = enricher._call_nvidia_embedding(query, cfg.nvidia_api_key, cfg.embedding_model, input_type="query", purpose="sandbox")
                
            if query_vector:
                q_vec = np.array(query_vector, dtype=np.float32)
                # Load all chunk embeddings matching the query vector's dimension
                all_chunks = db.get_all_chunks_with_embeddings(expected_dim=len(q_vec))

                if all_chunks:
                    chunk_embeddings = np.array([c["embedding"] for c in all_chunks], dtype=np.float32)
                    chunk_ids = [c["id"] for c in all_chunks]
                    
                    # Compute dot product and normalized cosine similarity
                    q_norm = np.linalg.norm(q_vec)
                    chunk_norms = np.linalg.norm(chunk_embeddings, axis=1)
                    
                    # Avoid division by zero
                    norms = q_norm * chunk_norms
                    norms[norms == 0] = 1.0
                    
                    cosine_similarities = np.dot(chunk_embeddings, q_vec) / norms
                    
                    for cid, score in zip(chunk_ids, cosine_similarities):
                        semantic_hits.append({"chunk_id": cid, "score": float(score)})
                    
                    semantic_hits.sort(key=lambda x: x["score"], reverse=True)
        except Exception as e:
            logger.warn(f"Semantic search failed, falling back to lexical search: {e}")
            
    # 3. Fuse Results (Hybrid search)
    # Simple reciprocal rank fusion or score combination
    combined_scores = {}
    
    # Add lexical scores (inverse of rank, rank starts at 0 or is rank-based)
    for idx, hit in enumerate(lexical_hits):
        cid = hit["chunk_id"]
        combined_scores[cid] = combined_scores.get(cid, 0.0) + (1.0 / (idx + 1) * 0.5)
        
    # Add semantic scores (scaled to 0-1)
    for idx, hit in enumerate(semantic_hits[:limit * 2]):
        cid = hit["chunk_id"]
        combined_scores[cid] = combined_scores.get(cid, 0.0) + (hit["score"] * 0.5)
        
    # Sort combined
    fused_chunk_ids = sorted(combined_scores.keys(), key=lambda x: combined_scores[x], reverse=True)[:limit]
    
    if not fused_chunk_ids:
        print("No matches found.")
        return
        
    # Load details for matched chunks
    with db.get_connection() as conn:
        print(f"\n{'Score':<6} | {'Doc':<20} | {'Pages':<6} | {'SummarySnippet'}")
        print("-" * 80)
        for cid in fused_chunk_ids:
            row = conn.execute("""
                SELECT c.id, c.page_start, c.page_end, c.summary, d.filename 
                FROM chunks c
                JOIN documents d ON c.document_id = d.id
                WHERE c.id = ?
            """, (cid,)).fetchone()
            if row:
                score = round(combined_scores[cid], 3)
                pages = f"{row['page_start']}-{row['page_end']}"
                snippet = row['summary'][:50].replace('\n', ' ')
                print(f"{score:<6} | {row['filename'][:20]:<20} | {pages:<6} | {snippet}...")
    print()

def main():
    parser = argparse.ArgumentParser(description="OpenPDFSpecs - Local PDF Ingestion and RAG CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # init
    subparsers.add_parser("init", help="Initialize the SQLite database schema")
    
    # ingest
    ingest_parser = subparsers.add_parser("ingest", help="Ingest a PDF file immediately")
    ingest_parser.add_argument("file_path", help="Path to the PDF file")
    
    # worker
    subparsers.add_parser("worker", help="Start the background ingestion worker loop")
    
    # ls
    subparsers.add_parser("ls", help="List all ingested documents")
    
    # status
    subparsers.add_parser("status", help="Show ingestion task queue status")
    
    # set-provider
    prov_parser = subparsers.add_parser("set-provider", help="Set the active API provider")
    prov_parser.add_argument("provider", choices=["gemini", "nvidia"], help="active provider")
    
    # set-key
    key_parser = subparsers.add_parser("set-key", help="Set the API key for the active provider")
    key_parser.add_argument("api_key", help="API Key")
    
    # search
    search_parser = subparsers.add_parser("search", help="Perform a hybrid test search")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--limit", type=int, default=5, help="Max results to display")
    
    args = parser.parse_args()
    
    if args.command == "init":
        handle_init(args)
    elif args.command == "ingest":
        handle_ingest(args)
    elif args.command == "worker":
        handle_worker(args)
    elif args.command == "ls":
        handle_ls(args)
    elif args.command == "status":
        handle_status(args)
    elif args.command == "set-provider":
        handle_set_provider(args)
    elif args.command == "set-key":
        handle_set_key(args)
    elif args.command == "search":
        handle_search(args)

if __name__ == "__main__":
    main()
