"""
MCP tool definitions, shared by both transports (REVAMP Phase B).

There are two MCP surfaces — HTTP Streamable (src/server.py, for remote clients) and
stdio (src/mcp_server.py, for a local Claude Code / Cursor / Claude Desktop install) —
and they must expose identical tools. Defining them once here and binding an
`org_resolver` per transport is what keeps them from drifting: HTTP resolves the org from
the request's API key, stdio from the local single-user org.
"""
import src.pgdb as pgdb
import src.search as search

# Delivered to the connecting AI on initialize, so it knows what this knowledgebase is and
# how to use the tools without any extra prompting.
MCP_INSTRUCTIONS = """\
OpenPDFSpecs is a searchable knowledgebase of classical Ayurvedic texts (e.g. Charaka \
Samhita, Sushruta Samhita, Ashtanga Hridayam/Sangraha by Vagbhata, Yogaratnakara).

Use these tools to answer questions grounded in the source texts:
- search_knowledge(query, limit=5, max 50): hybrid lexical + semantic search. Best first step for
  any question. Query by symptom, condition, treatment, herb, or concept (e.g. "treatment
  for fever", "jwara", "uses of guduchi"). Returns matching passages with a summary, the
  source file, page range, and a chunk_id.
- retrieve_chunk(chunk_id): read the full text of a specific chunk returned by a search.
- entity_lookup(entity_name): find every passage where a concept, herb, person, or term
  appears across the library.
- list_documents_tool(): list the source documents available.

Guidance: search first, then retrieve_chunk for detail before answering. Ground answers in
retrieved content and cite the source file and page range. If nothing relevant is found,
say so rather than guessing. These are historical texts, not medical advice.
"""

# The `limit` bounds live in src/search.py, next to the candidate-pool arithmetic they
# protect, so the REST transport enforces exactly the same cap as these tools did.
MAX_SEARCH_LIMIT = search.MAX_SEARCH_LIMIT


def register_tools(mcp, org_resolver):
    """
    Register the four knowledgebase tools on a FastMCP instance.

    Args:
        mcp: the FastMCP server to register on.
        org_resolver: zero-arg callable returning the org_id for the current request.
    """

    @mcp.tool()
    def list_documents_tool() -> str:
        """List all indexed documents available in this knowledgebase."""
        docs = pgdb.list_documents(org_resolver())
        if not docs:
            return "No documents have been indexed yet."
        lines = ["# Documents", "", "| ID | Filename | Size (KB) | Ingested |", "|---|---|---|---|"]
        for d in docs:
            lines.append(f"| `{d['id']}` | {d['filename']} | {round(d['size_bytes']/1024,1)} | {d['created_at']} |")
        return "\n".join(lines)

    @mcp.tool()
    def search_knowledge(query: str, limit: int = search.DEFAULT_SEARCH_LIMIT) -> str:
        """Hybrid (lexical + semantic) search over the knowledgebase. Returns matching chunks."""
        limit = search.clamp_limit(limit)
        rows = search.hybrid_search(org_resolver(), query, limit)
        if not rows:
            return f"No matches for: '{query}'."
        out = [f"# Search Results for: '{query}'", ""]
        for i, r in enumerate(rows):
            snippet = (r["text_content"] or "")[:400].replace("\n", " ")
            out += [f"### {i+1}. {r['filename']} (pages {r['page_start']}-{r['page_end']})",
                    f"- Chunk ID: `{r['id']}`",
                    f"- Score: {round(r['score'],4)}",
                    f"- Summary: {r.get('summary','')}",
                    f"- Snippet: \"{snippet}...\"", ""]
        return "\n".join(out)

    @mcp.tool()
    def retrieve_chunk(chunk_id: str) -> str:
        """Retrieve the full text of a chunk by its ID."""
        # chunk_id arrives straight from a model, which will occasionally invent one.
        # chunks.id is a uuid column, so a malformed value used to reach Postgres and come
        # back as an unhandled `invalid input syntax for type uuid` out of the tool.
        if not pgdb.is_valid_uuid(chunk_id):
            return (f"`{chunk_id}` is not a valid chunk id. Chunk ids are UUIDs that come "
                    f"from search_knowledge — run a search first and pass one of the "
                    f"`Chunk ID` values from its results.")
        row = pgdb.get_chunk(org_resolver(), chunk_id)
        if not row:
            return (f"Chunk `{chunk_id}` not found. It may belong to a document that was "
                    f"deleted; run search_knowledge again for current chunk ids.")
        return "\n".join([f"# {row['filename']}",
                          f"Pages {row['page_start']}-{row['page_end']}", "",
                          "## Summary", row.get("summary") or "", "",
                          "## Content", "```text", row["text_content"], "```"])

    @mcp.tool()
    def entity_lookup(entity_name: str) -> str:
        """Find chunks/documents associated with an entity, concept, or term."""
        rows = pgdb.entity_lookup(org_resolver(), entity_name)
        if not rows:
            return f"No occurrences found for entity: '{entity_name}'."
        out = [f"# Occurrences of '{entity_name}'", "",
               "| Document | Pages | Entity (Type) | Chunk |", "|---|---|---|---|"]
        for r in rows:
            summ = (r.get("summary") or "")[:60].replace("\n", " ")
            out.append(f"| {r['filename']} | {r['page_start']}-{r['page_end']} | "
                       f"{r['entity_name']} ({r['entity_type']}) | `{r['chunk_id']}`: {summ}... |")
        return "\n".join(out)

    return mcp
