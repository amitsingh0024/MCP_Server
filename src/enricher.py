import json
import logging
import time
from dataclasses import dataclass
import requests
from requests.exceptions import RequestException, HTTPError
from src.config import get_config

logger = logging.getLogger("pdfspecs.enricher")

class APIKeyMissingError(ValueError):
    pass


@dataclass
class ProviderSettings:
    """Explicit provider config for a single enrichment call.

    Mirrors the attribute names on the global Config so either can be passed as
    `settings`. Used to run enrichment with a specific org's BYO key in the
    multi-tenant worker instead of reading process-global config.
    """
    provider: str = "gemini"
    gemini_api_key: str = ""
    nvidia_api_key: str = ""
    llm_model: str = "gemini-2.5-flash"
    embedding_model: str = "text-embedding-004"


def _record_usage(usage_sink, purpose, prompt_tokens, completion_tokens):
    """Record token usage via the injected sink (org-scoped). No-op without a sink."""
    if not (prompt_tokens or completion_tokens) or usage_sink is None:
        return
    try:
        usage_sink(purpose, prompt_tokens, completion_tokens)
    except Exception as e:
        logger.error(f"Failed to record token usage: {e}")

def _post_with_retry(url, json_payload, headers, max_retries=5, backoff_factor=1.5):
    """Executes a POST request with exponential backoff for transient errors."""
    for attempt in range(max_retries):
        try:
            response = requests.post(url, json=json_payload, headers=headers, timeout=45)
            response.raise_for_status()
            return response
        except RequestException as e:
            if attempt == max_retries - 1:
                raise
            
            # Don't retry on client errors (except 429 Too Many Requests)
            if isinstance(e, HTTPError) and e.response is not None:
                status = e.response.status_code
                if 400 <= status < 500 and status != 429:
                    raise
            
            sleep_time = backoff_factor * (2 ** attempt)
            logger.warning(f"API request failed: {e}. Retrying in {sleep_time}s (Attempt {attempt + 1}/{max_retries})...")
            time.sleep(sleep_time)

def _call_gemini_llm(text: str, api_key: str, model: str, usage_sink=None) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    
    prompt = f"""Analyze the following text chunk from a document. Extract:
1. A concise 1-2 sentence summary of the key information.
2. Up to 5 keywords or key topics.
3. Important entities mentioned (e.g. key books, references, people, organizations, locations, concepts). Entity names should be normalized to their canonical forms.

Return the output strictly in the following JSON format:
{{
  "summary": "...",
  "keywords": ["...", "..."],
  "entities": [
    {{"name": "Entity Name", "type": "concept"}},
    {{"name": "Another Entity", "type": "person"}}
  ]
}}
Valid types for entities are: "concept", "organization", "person", "location".

Text chunk:
{text}
"""
    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }
    
    headers = {"Content-Type": "application/json"}
    response = _post_with_retry(url, json_payload=payload, headers=headers)
    
    data = response.json()
    
    # Record token usage
    usage = data.get("usageMetadata", {})
    _record_usage(usage_sink, "ingestion",
                  usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0))

    try:
        content_str = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(content_str)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        logger.error(f"Failed to parse Gemini response: {e}. Raw response: {data}")
        raise ValueError("Invalid response format from Gemini API")

def _call_nvidia_llm(text: str, api_key: str, model: str, usage_sink=None) -> dict:
    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    
    prompt = f"""Analyze the following text chunk from a document. Extract:
1. A concise 1-2 sentence summary of the key information.
2. Up to 5 keywords or key topics.
3. Important entities mentioned (e.g. key books, references, people, organizations, locations, concepts). Entity names should be normalized to their canonical forms.

Return the output strictly in the following JSON format:
{{
  "summary": "...",
  "keywords": ["...", "..."],
  "entities": [
    {{"name": "Entity Name", "type": "concept"}},
    {{"name": "Another Entity", "type": "person"}}
  ]
}}
Valid types for entities are: "concept", "organization", "person", "location".

Text chunk:
{text}
"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "response_format": {"type": "json_object"}
    }
    
    response = _post_with_retry(url, json_payload=payload, headers=headers)
    
    data = response.json()
    
    # Record token usage
    usage = data.get("usage", {})
    _record_usage(usage_sink, "ingestion",
                  usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))

    try:
        content_str = data["choices"][0]["message"]["content"]
        return json.loads(content_str)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        logger.error(f"Failed to parse Nvidia response: {e}. Raw response: {data}")
        raise ValueError("Invalid response format from Nvidia NIM API")

def _call_gemini_embedding(text: str, api_key: str, model: str) -> list:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent?key={api_key}"
    payload = {
        "content": {
            "parts": [{"text": text}]
        }
    }
    headers = {"Content-Type": "application/json"}
    response = _post_with_retry(url, json_payload=payload, headers=headers)
    
    data = response.json()
    try:
        return data["embedding"]["values"]
    except KeyError as e:
        logger.error(f"Failed to extract embedding from Gemini: {e}. Raw response: {data}")
        raise ValueError("Invalid embedding response format from Gemini API")

def _call_nvidia_embedding(text: str, api_key: str, model: str, input_type: str = "passage", purpose: str = "ingestion", usage_sink=None) -> list:
    url = "https://integrate.api.nvidia.com/v1/embeddings"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    # Truncate to safe character limit (1000 characters) to prevent 400 Bad Request
    # 1000 characters is guaranteed to be under 512 tokens even for symbol-dense markdown tables
    if len(text) > 1000:
        safe_text = text[:1000]
        logger.warning(f"Truncated embedding input from {len(text)} to 1000 characters to avoid NVIDIA 512 token limit.")
    else:
        safe_text = text

    payload = {
        "input": [safe_text],
        "model": model,
        "encoding_format": "float",
        "input_type": input_type
    }
    response = _post_with_retry(url, json_payload=payload, headers=headers)
    
    data = response.json()
    try:
        embedding = data["data"][0]["embedding"]
        usage = data.get("usage", {})
        _record_usage(usage_sink, purpose, usage.get("prompt_tokens", 0), 0)
        return embedding
    except (KeyError, IndexError) as e:
        logger.error(f"Failed to extract embedding from Nvidia: {e}. Raw response: {data}")
        raise ValueError("Invalid embedding response format from Nvidia NIM API")


def _call_gemini_synthesis(prompt: str, api_key: str, model: str, usage_sink=None) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }]
    }
    headers = {"Content-Type": "application/json"}
    try:
        response = _post_with_retry(url, json_payload=payload, headers=headers)
        data = response.json()
        
        usage = data.get("usageMetadata", {})
        _record_usage(usage_sink, "sandbox",
                      usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0))
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        logger.error(f"Gemini synthesis failed: {e}")
        return f"Failed to generate synthesis due to API error: {e}"


def _call_nvidia_synthesis(prompt: str, api_key: str, model: str, usage_sink=None) -> str:
    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt}
        ]
    }
    try:
        response = _post_with_retry(url, json_payload=payload, headers=headers)
        data = response.json()
        
        usage = data.get("usage", {})
        _record_usage(usage_sink, "sandbox",
                      usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))

        return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"Nvidia synthesis failed: {e}")
        return f"Failed to generate synthesis due to API error: {e}"


def generate_rag_response(query: str, retrieved_chunks: list) -> str:
    config = get_config()
    
    # Format the context from retrieved chunks
    context_parts = []
    for idx, chunk in enumerate(retrieved_chunks):
        filename = chunk.get("filename", "Unknown")
        pages = f"Pages {chunk.get('page_start', '?')}-{chunk.get('page_end', '?')}"
        text = chunk.get("text_content", "")
        summary = chunk.get("summary", "")
        context_parts.append(
            f"--- Reference {idx+1} ({filename}, {pages}) ---\n"
            f"Summary: {summary}\n"
            f"Content:\n{text}\n"
        )
    context_str = "\n".join(context_parts)
    
    prompt = f"""You are an expert Ayurvedic practitioner assistant. You are helping a practitioner assess a patient's condition or query based on historical Ayurvedic texts.

Query/Condition to assess:
{query}

Retrieved context from documents:
{context_str}

Please synthesize a clear, professional, and user-readable clinical summary/assessment of the condition. Follow this structure:
1. **Clinical Assessment / Etiology**: Explain the condition, its causes, and symptoms as described in the retrieved Sanskrit slokas and text.
2. **Treatment & Recommendations**: Detail any remedies, herbs, dietary rules, or therapies suggested.
3. **Key Slokas & References**: List/quote the relevant Sanskrit slokas or direct statements, mapping them to their source files/page numbers.

Guidelines:
- Keep the language professional, empathetic, and clear for a modern practitioner.
- Use only the provided context. Do not invent details not mentioned in the context.
- If the context doesn't contain relevant information, state that clearly and suggest what might be missing.

Response:"""

    if config.provider == "gemini":
        if not config.gemini_api_key:
            return "Error: Gemini API key is missing. Please configure it in settings."
        return _call_gemini_synthesis(prompt, config.gemini_api_key, config.llm_model)
    elif config.provider == "nvidia":
        if not config.nvidia_api_key:
            return "Error: Nvidia API key is missing. Please configure it in settings."
        return _call_nvidia_synthesis(prompt, config.nvidia_api_key, config.llm_model)
    else:
        return f"Error: Unsupported provider: {config.provider}"


def enrich_chunk(text: str, settings=None, usage_sink=None) -> dict:
    """
    Calls the LLM and Embedding provider to extract metadata and compute a vector
    for a chunk of text.

    Args:
        text: the chunk text.
        settings: an optional ProviderSettings (or Config) supplying provider/keys/models.
            When None, falls back to the process-global config (legacy single-user path).
            The multi-tenant worker passes a per-org ProviderSettings here (BYO key).
        usage_sink: optional callable(purpose, prompt_tokens, completion_tokens) to record
            token usage (e.g. org-scoped via pgdb). When None, the legacy SQLite recorder is used.

    Returns dict: {summary, keywords, entities, embedding, embedding_model}
    """
    config = settings or get_config()

    # 1. Generate Metadata
    metadata = {}
    if config.provider == "gemini":
        if not config.gemini_api_key:
            raise APIKeyMissingError("Gemini API key is missing. Please set it in config.json or GEMINI_API_KEY environment variable.")
        metadata = _call_gemini_llm(text, config.gemini_api_key, config.llm_model, usage_sink=usage_sink)
    elif config.provider == "nvidia":
        if not config.nvidia_api_key:
            raise APIKeyMissingError("Nvidia API key is missing. Please set it in config.json or NVIDIA_API_KEY environment variable.")
        metadata = _call_nvidia_llm(text, config.nvidia_api_key, config.llm_model, usage_sink=usage_sink)
    else:
        raise ValueError(f"Unsupported provider: {config.provider}")

    # Ensure format consistency
    summary = metadata.get("summary", "")
    keywords = metadata.get("keywords", [])
    entities = metadata.get("entities", [])

    # Clean up entities (normalize types and names)
    cleaned_entities = []
    valid_types = {"concept", "organization", "person", "location"}
    for ent in entities:
        name = ent.get("name", "").strip()
        etype = ent.get("type", "").strip().lower()
        if etype not in valid_types:
            etype = "concept"
        if name:
            cleaned_entities.append({"name": name, "type": etype})

    # 2. Generate Embedding
    embedding = None
    embedding_model = None
    try:
        if config.provider == "gemini":
            embedding = _call_gemini_embedding(text, config.gemini_api_key, config.embedding_model)
        elif config.provider == "nvidia":
            embedding = _call_nvidia_embedding(text, config.nvidia_api_key, config.embedding_model, input_type="passage", usage_sink=usage_sink)
        if embedding:
            embedding_model = config.embedding_model
    except Exception as e:
        logger.error(f"Failed to generate embedding for chunk: {e}")
        # Return empty list or fallback to None. RAG will fallback to lexical search if embedding is missing
        embedding = []

    return {
        "summary": summary,
        "keywords": keywords,
        "entities": cleaned_entities,
        "embedding": embedding,
        "embedding_model": embedding_model,
    }
