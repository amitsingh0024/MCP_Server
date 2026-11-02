import os
import json
from pathlib import Path

# Resolve config.json relative to the project root (this file lives in <root>/src/).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
APP_DIR = Path.home() / ".pdfspecs"


def load_dotenv_if_present(path: Path = None) -> bool:
    """
    Load `KEY=value` lines from the project's .env into os.environ (without overriding
    anything already set).

    The local entrypoints (CLI, stdio MCP server) are launched directly or by an MCP
    client, neither of which sources a shell profile — so without this, DATABASE_URL and
    friends would have to be repeated in every client config. Deliberately minimal: no
    interpolation, no export syntax, no new dependency.
    """
    path = path or (PROJECT_ROOT / ".env")
    if not path.is_file():
        return False
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    return True

# Keys that are secrets: kept Fernet-encrypted in the Postgres `org_settings` table
# (see pgdb.save_setting(encrypt=True)), never written back to config.json on disk.
SECRET_KEYS = {"nvidia_api_key"}


class Config:
    def __init__(self, config_path: Path = DEFAULT_CONFIG_PATH):
        self.config_path = config_path
        self.settings = {
            "nvidia_api_key": "",
            "llm_model": "nvidia/nemotron-3.5-lightning-30b-a3b",
            "embedding_model": "BAAI/bge-m3",
            "chunk_size": 2500,
            "chunk_overlap": 250,
            "ocr_mode": "auto",        # auto | always | never
            "ocr_min_chars": 40,        # in auto mode, OCR a page whose text layer has fewer chars than this
            "ocr_provider": "paddle",   # paddle | tesseract
            "max_upload_mb": 50,        # reject uploads larger than this
            # Parallel enrichment calls. 1 by default: the global request throttle in
            # enricher already caps the rate, so extra workers only queue on it. Raise it
            # when the provider key has generous limits.
            "enrich_concurrency": 1,
            "max_task_attempts": 3,     # mark a task failed after this many attempts
        }
        self.load()

    def load(self):
        # 1. Load from config.json if it exists
        if self.config_path.exists():
            try:
                with open(self.config_path, "r") as f:
                    user_settings = json.load(f)
                    self.settings.update(user_settings)
            except Exception as e:
                print(f"Warning: Failed to load config.json: {e}")

        # 2. Environment variable overrides (highest precedence)
        #
        # These are PLATFORM settings (chunking, OCR, upload limits) — deliberately not
        # per-org. Per-org values (the NVIDIA key, llm_model) live in the org_settings
        # table and are read through pgdb, never from here.
        #
        # This used to also read a SQLite database at ~/.pdfspecs/data.db, left over from
        # before the Postgres migration. Nothing wrote to it any more, so it was a second
        # source of truth that could only ever be stale (REVAMP Phase B).
        self.nvidia_api_key = os.getenv("NVIDIA_API_KEY", self.settings.get("nvidia_api_key", ""))
        self.llm_model = os.getenv("PDFSPECS_LLM_MODEL", self.settings.get("llm_model", "nvidia/nemotron-3.5-lightning-30b-a3b"))
        self.embedding_model = os.getenv("PDFSPECS_EMBEDDING_MODEL", self.settings.get("embedding_model", "BAAI/bge-m3"))
        self.chunk_size = int(os.getenv("PDFSPECS_CHUNK_SIZE", self.settings.get("chunk_size", 2500)))
        self.chunk_overlap = int(os.getenv("PDFSPECS_CHUNK_OVERLAP", self.settings.get("chunk_overlap", 250)))

        ocr_mode = os.getenv("PDFSPECS_OCR_MODE", self.settings.get("ocr_mode", "auto")).lower()
        self.ocr_mode = ocr_mode if ocr_mode in ("auto", "always", "never") else "auto"
        self.ocr_min_chars = int(os.getenv("PDFSPECS_OCR_MIN_CHARS", self.settings.get("ocr_min_chars", 40)))
        
        ocr_provider = os.getenv("PDFSPECS_OCR_PROVIDER", self.settings.get("ocr_provider", "paddle")).lower()
        self.ocr_provider = ocr_provider if ocr_provider in ("paddle", "tesseract") else "paddle"
        
        self.max_upload_mb = int(os.getenv("PDFSPECS_MAX_UPLOAD_MB", self.settings.get("max_upload_mb", 50)))
        self.enrich_concurrency = max(1, int(os.getenv("PDFSPECS_ENRICH_CONCURRENCY", self.settings.get("enrich_concurrency", 4))))
        self.max_task_attempts = max(1, int(os.getenv("PDFSPECS_MAX_TASK_ATTEMPTS", self.settings.get("max_task_attempts", 3))))

    def save(self):
        """Persist NON-SECRET platform settings to config.json.

        API keys are intentionally never written here; the per-org NVIDIA key lives
        Fernet-encrypted in the `org_settings` table (see pgdb.save_setting(encrypt=True)).
        """
        on_disk = {
            "llm_model": self.llm_model,
            "embedding_model": self.embedding_model,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "ocr_mode": self.ocr_mode,
            "ocr_min_chars": self.ocr_min_chars,
            "ocr_provider": self.ocr_provider,
            "max_upload_mb": self.max_upload_mb,
            "enrich_concurrency": self.enrich_concurrency,
            "max_task_attempts": self.max_task_attempts,
        }
        self.settings.update(on_disk)
        # Make sure no secret ever lands in the file.
        for secret in SECRET_KEYS:
            self.settings.pop(secret, None)
        try:
            with open(self.config_path, "w") as f:
                json.dump(on_disk, f, indent=2)
        except Exception as e:
            print(f"Error: Failed to save config.json: {e}")


_config_instance = None


def get_config() -> Config:
    global _config_instance
    if _config_instance is None:
        _config_instance = Config()
    return _config_instance
