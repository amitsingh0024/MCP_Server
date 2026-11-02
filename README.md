# OpenPDFSpecs (PDF RAG over MCP)

OpenPDFSpecs is a Python document knowledge engine. It parses, chunks, and enriches PDF files using free-tier LLM/embedding APIs, and exposes the resulting knowledgebase to AI assistants (Cursor, Claude Code, etc.) over the Model Context Protocol (MCP).

> ### ⚠️ This README is out of date
>
> It describes the original single-user SQLite build. The project has since moved to
> multi-tenant Postgres (Supabase) + an HTTP MCP endpoint, and `src/cli.py`,
> `src/mcp_server.py`, `src/db.py` and `src/queue.py` **no longer exist** — every CLI
> command below is currently non-functional.
>
> For how things actually work today see **[docs/AUDIT.md](docs/AUDIT.md)** (current state)
> and **[docs/REVAMP.md](docs/REVAMP.md)** (what's changing). This file is rewritten in
> Phase B, when the local-first / stdio story lands.

---

## Features

- **Blazing Fast Local PDF Parsing**: Uses `PyMuPDF` (C-based engine) to parse text and detect tables, formatting tables as clean Markdown.
- **Relational Graph Database**: Models document structures and semantic entities in a local, zero-dependency `SQLite` database.
- **Hybrid Search**: Fuses SQLite FTS5 lexical text search with fast in-memory NumPy cosine similarity of embeddings.
- **Free API Tier Integration**: Uses the Nvidia Developer NIM API (`meta/llama-3.1-8b-instruct` for extraction, `nvidia/nv-embedqa-e5-v5` for embeddings) for cost-free enrichment.
- **Background Async Ingestion**: Queues PDF tasks and processes them incrementally (skips unchanged PDFs using SHA-256 diff checks).
- **FastMCP Server**: Standard stdio-based MCP server containing search, retrieve, and entity lookup tools.

---

## Setup & Installation

### 1. Prerequisites
- Python 3.14+
- An Nvidia NIM API Key (`nvapi-...`) from the Nvidia Developer program

### 2. Setup Virtual Environment
Initialize a Python virtual environment and install dependencies:
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. Initialize Database
Initialize the SQLite schema:
```bash
.venv/bin/python src/cli.py init
```

---

## Configuration

Settings are stored in `config.json`. You can modify it directly or use the CLI.

### Set API Key
Provide your developer API key:
```bash
.venv/bin/python src/cli.py set-key YOUR_API_KEY
```

---

## Usage

### Ingest a PDF Document
To add a document and run the parsing/LLM-enrichment synchronously:
```bash
.venv/bin/python src/cli.py ingest /path/to/your/document.pdf
```

### Run background worker
To run a worker that polls the SQLite queue for pending documents:
```bash
.venv/bin/python src/cli.py worker
```

### List Cataloged Documents
View all processed files:
```bash
.venv/bin/python src/cli.py ls
```

### View Ingestion Status
Check the status of queue tasks:
```bash
.venv/bin/python src/cli.py status
```

### Run Direct RAG Search
Perform a hybrid search directly in the terminal to test RAG retrieval:
```bash
.venv/bin/python src/cli.py search "caching strategy"
```

---

## Connecting to MCP Clients

Since OpenPDFSpecs runs as an MCP server over stdio, it doesn't consume ports or require server hosting costs.

### Connect to Claude Code
Add the MCP server to Claude Code:
```bash
claude mcp add openpdfspecs .venv/bin/python src/mcp_server.py
```

### Connect to Cursor / Claude Desktop
Add the server config in Cursor (`Settings -> MCP`) or Claude Desktop configuration (`config.json`):
```json
{
  "mcpServers": {
    "openpdfspecs": {
      "command": "/absolute/path/to/workspace/.venv/bin/python",
      "args": [
        "/absolute/path/to/workspace/src/mcp_server.py"
      ],
      "env": {
        "NVIDIA_API_KEY": "YOUR_NVIDIA_KEY"
      }
    }
  }
}
```

---

## MCP Tools Exposed

1. `list_documents()`: Lists cataloged documents in the knowledgebase.
2. `search_knowledge(query, limit)`: Performs a hybrid FTS5 and vector similarity search.
3. `retrieve_chunk(chunk_id)`: Fetches the raw text content of a chunk.
4. `entity_lookup(entity_name)`: Relational lookup mapping concepts, books, and references to pages.
