# OpenPDFSpecs — Connect & Query (for AI tools)

OpenPDFSpecs is a private, searchable knowledgebase of classical **Ayurvedic texts**
(Charaka Samhita, Sushruta Samhita, Ashtanga Hridayam/Sangraha by Vagbhata, Yogaratnakara).
It's exposed to AI assistants over **MCP (Model Context Protocol)**, so tools like Claude and
Cursor can search it and answer questions grounded in the source texts.

Access is by **API key** — each key is scoped to one organization's data.

---

## 1. Connect your AI tool

You need the connection URL (from the dashboard's **API Keys** panel):

```
https://openpdfspecs-api.onrender.com/mcp?token=<YOUR_API_KEY>
```

The token in the URL authenticates you — no extra headers needed. Add it as a **custom /
remote MCP connector**:

| Tool | How to add it |
|---|---|
| **claude.ai / Claude Desktop** | Settings → Connectors → **Add custom connector** → paste the URL |
| **Claude Code (CLI)** | `claude mcp add --transport http openpdfspecs "https://openpdfspecs-api.onrender.com/mcp?token=<YOUR_API_KEY>"` |
| **Cursor** (`~/.cursor/mcp.json`) | `{ "mcpServers": { "openpdfspecs": { "url": "https://openpdfspecs-api.onrender.com/mcp?token=<YOUR_API_KEY>" } } }` |

Once connected, the assistant sees four tools (below) and a built-in description of the
knowledgebase.

---

## 2. What the AI can do (tools)

- **`search_knowledge(query, limit=5)`** — the main tool. Hybrid lexical + semantic search.
  Query by symptom, condition, treatment, herb, or concept. Returns matching passages with a
  summary, source file, page range, and a `chunk_id`.
- **`retrieve_chunk(chunk_id)`** — read the full text of a chunk returned by a search.
- **`entity_lookup(entity_name)`** — find every passage mentioning a concept, herb, person,
  or term across the whole library.
- **`list_documents_tool()`** — list the source documents available.

**Typical flow the AI follows:** `search_knowledge` → `retrieve_chunk` (for detail) → answer,
citing the source file and page range.

---

## 3. How to ask (examples)

Just ask questions in natural language — the AI picks the tools. For example:

- "What does Ayurveda say about the treatment of fever (jwara)?"
- "Find remedies for cough and dyspnoea and cite the source."
- "Where is guduchi (Tinospora) mentioned, and for what?"
- "Summarize the dietary rules for a Pitta imbalance from these texts."

The assistant will search, pull the relevant passages, and answer **grounded in the texts**,
citing file and page. If nothing relevant exists in the corpus, it will say so rather than
guess.

---

## 4. Good to know

- **Data scope:** answers come only from the ingested Ayurvedic PDFs in your organization —
  not the open web. These are historical texts, **not medical advice**.
- **First request may be slow:** the service is on a free tier that sleeps after ~15 min
  idle; the first query after a quiet period can take 30–60s to wake. Just retry.
- **Your key is a credential:** anyone with the URL can query your org's data. Share it
  privately; revoke a key in the dashboard if it leaks.
