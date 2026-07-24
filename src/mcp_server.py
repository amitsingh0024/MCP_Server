import json
import logging
import numpy as np
from mcp.server.fastmcp import FastMCP

import src.db as db
import src.config as config
import src.enricher as enricher

# Configure logging to sys.stderr (mcp stdio transport requires stdout to be clean JSON-RPC)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(import_sys := __import__("sys").stderr)]
)
logger = logging.getLogger("pdfspecs.mcp")

# Initialize FastMCP server
mcp = FastMCP("OpenPDFSpecs")

@mcp.tool()
def list_documents() -> str:
    """
    Lists all indexed documents and books in the knowledgebase with their metadata.
    Use this to see what documents are available to query.
    """
    db.init_db()
    docs = db.list_documents()
    if not docs:
        return "No documents have been indexed yet. Use the CLI `python3 src/cli.py ingest <file_path>` to add documents."
        
    lines = ["# Ingested Documents", ""]
    lines.append("| Document ID | Filename | Size (KB) | Ingested At |")
    lines.append("|---|---|---|---|")
    for doc in docs:
        size_kb = round(doc["size_bytes"] / 1024, 1)
        lines.append(f"| `{doc['id']}` | **{doc['filename']}** | {size_kb} | {doc['created_at']} |")
    return "\n".join(lines)

def retrieve_chunks_for_query(query: str, limit: int = 5) -> list:
    db.init_db()
    cfg = config.get_config()
    
    # 1. Lexical search (FTS5)
    lexical_hits = db.query_lexical_fts(query, limit=limit * 2)
    
    # 2. Semantic search
    semantic_hits = []
    has_key = (cfg.provider == "gemini" and cfg.gemini_api_key) or (cfg.provider == "nvidia" and cfg.nvidia_api_key)
    
    if has_key:
        try:
            query_vector = None
            if cfg.provider == "gemini":
                query_vector = enricher._call_gemini_embedding(query, cfg.gemini_api_key, cfg.embedding_model)
            elif cfg.provider == "nvidia":
                query_vector = enricher._call_nvidia_embedding(query, cfg.nvidia_api_key, cfg.embedding_model, input_type="query", purpose="sandbox")
                
            if query_vector:
                q_vec = np.array(query_vector, dtype=np.float32)
                all_chunks = db.get_all_chunks_with_embeddings(expected_dim=len(q_vec))

                if all_chunks:
                    chunk_embeddings = np.array([c["embedding"] for c in all_chunks], dtype=np.float32)
                    chunk_ids = [c["id"] for c in all_chunks]
                    
                    # Compute cosine similarity
                    q_norm = np.linalg.norm(q_vec)
                    chunk_norms = np.linalg.norm(chunk_embeddings, axis=1)
                    norms = q_norm * chunk_norms
                    norms[norms == 0] = 1.0
                    
                    cosine_similarities = np.dot(chunk_embeddings, q_vec) / norms
                    
                    for cid, score in zip(chunk_ids, cosine_similarities):
                        semantic_hits.append({"chunk_id": cid, "score": float(score)})
                    
                    semantic_hits.sort(key=lambda x: x["score"], reverse=True)
        except Exception as e:
            logger.error(f"Semantic vector search error: {e}")

    # 3. Hybrid fusion
    combined_scores = {}
    for idx, hit in enumerate(lexical_hits):
        cid = hit["chunk_id"]
        combined_scores[cid] = combined_scores.get(cid, 0.0) + (1.0 / (idx + 1) * 0.5)
        
    for idx, hit in enumerate(semantic_hits[:limit * 2]):
        cid = hit["chunk_id"]
        combined_scores[cid] = combined_scores.get(cid, 0.0) + (hit["score"] * 0.5)
        
    fused_chunk_ids = sorted(combined_scores.keys(), key=lambda x: combined_scores[x], reverse=True)[:limit]
    
    results = []
    if not fused_chunk_ids:
        return results

    with db.get_connection() as conn:
        for cid in fused_chunk_ids:
            row = conn.execute("""
                SELECT c.id, c.page_start, c.page_end, c.summary, c.text_content, d.filename 
                FROM chunks c
                JOIN documents d ON c.document_id = d.id
                WHERE c.id = ?
            """, (cid,)).fetchone()
            
            if row:
                d = dict(row)
                d["score"] = combined_scores[cid]
                results.append(d)
    return results

@mcp.tool()
def search_knowledge(query: str, limit: int = 5) -> str:
    """
    Searches the document knowledgebase using hybrid lexical (BM25) and semantic vector search.
    Returns matched text chunks, summaries, and page ranges.
    
    Args:
        query (str): The search query.
        limit (int): Max number of matching chunks to return (default 5).
    """
    chunks = retrieve_chunks_for_query(query, limit)
    if not chunks:
        return f"No matches found for search query: '{query}'."

    # 4. Format Output
    output_parts = [f"# Search Results for: '{query}'", ""]
    for idx, row in enumerate(chunks):
        score = round(row["score"], 3)
        output_parts.append(f"### {idx+1}. {row['filename']} (Pages {row['page_start']}-{row['page_end']})")
        output_parts.append(f"- **Chunk ID**: `{row['id']}`")
        output_parts.append(f"- **Relevance Score**: `{score}`")
        output_parts.append(f"- **Summary**: {row['summary']}")
        # Include a text snippet
        snippet = row['text_content'][:400].replace('\n', ' ')
        output_parts.append(f"- **Text Snippet**: \"{snippet}...\"")
        output_parts.append("")
                
    output_parts.append("> [!TIP]")
    output_parts.append("> Use `retrieve_chunk(chunk_id=\"...\")` to read the full context of a matching chunk.")
    
    return "\n".join(output_parts)

@mcp.tool()
def retrieve_chunk(chunk_id: str) -> str:
    """
    Retrieves the full, raw text content of a chunk by its ID.
    Use this to read the details of a section when performing research.
    """
    db.init_db()
    with db.get_connection() as conn:
        row = conn.execute("""
            SELECT c.text_content, c.page_start, c.page_end, c.summary, d.filename 
            FROM chunks c
            JOIN documents d ON c.document_id = d.id
            WHERE c.id = ?
        """, (chunk_id,)).fetchone()
        
        if not row:
            return f"Error: Chunk with ID `{chunk_id}` not found."
            
        output = [
            f"# Document: {row['filename']}",
            f"**Pages**: {row['page_start']} - {row['page_end']}",
            "",
            "## Summary",
            row['summary'],
            "",
            "## Content",
            "```text",
            row['text_content'],
            "```"
        ]
        return "\n".join(output)

@mcp.tool()
def entity_lookup(entity_name: str) -> str:
    """
    Performs a graph search to find all chunks and documents associated with a specific entity, concept, or topic.
    Useful for checking where a specific book, reference, person, or term is mentioned in the entire library.
    """
    db.init_db()
    sanitized_id = f"%{entity_name.strip().lower()}%"
    
    with db.get_connection() as conn:
        rows = conn.execute("""
            SELECT c.id as chunk_id, c.page_start, c.page_end, c.summary, d.filename, e.name as entity_name, e.type as entity_type
            FROM entities e
            JOIN chunk_entities ce ON e.id = ce.entity_id
            JOIN chunks c ON ce.chunk_id = c.id
            JOIN documents d ON c.document_id = d.id
            WHERE e.name LIKE ? OR e.id LIKE ?
            ORDER BY d.filename, c.page_start
            LIMIT 20
        """, (sanitized_id, sanitized_id)).fetchall()
        
        if not rows:
            return f"No occurrences found for entity: '{entity_name}'."
            
        output = [f"# Occurrences of Entity: '{entity_name}'", ""]
        output.append("| Document | Pages | Entity Matched (Type) | Chunk ID & Summary |")
        output.append("|---|---|---|---|")
        for r in rows:
            summary_snip = r["summary"][:60].replace('\n', ' ')
            output.append(
                f"| {r['filename']} | {r['page_start']}-{r['page_end']} | {r['entity_name']} ({r['entity_type']}) | `{r['chunk_id']}`: {summary_snip}... |"
            )
            
        return "\n".join(output)

if __name__ == "__main__":
    mcp.run()
