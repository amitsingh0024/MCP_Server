import os
import json
import sqlite3
from pathlib import Path

# Resolve config.json relative to the project root (this file lives in <root>/src/).
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
APP_DIR = Path.home() / ".pdfspecs"

# Keys that are secrets: kept only in the encrypted SQLite `settings` table,
# never written back to config.json on disk.
SECRET_KEYS = {"gemini_api_key", "nvidia_api_key"}


class Config:
    def __init__(self, config_path: Path = DEFAULT_CONFIG_PATH):
        self.config_path = config_path
        self.settings = {
            "db_path": "~/.pdfspecs/data.db",
            "provider": "gemini",
            "gemini_api_key": "",
            "nvidia_api_key": "",
            "llm_model": "gemini-2.5-flash",
            "embedding_model": "text-embedding-004",
            "chunk_size": 2500,
            "chunk_overlap": 250,
            "ocr_mode": "auto",        # auto | always | never
            "ocr_min_chars": 40,        # in auto mode, OCR a page whose text layer has fewer chars than this
            "max_upload_mb": 50,        # reject uploads larger than this
            "enrich_concurrency": 4,    # parallel LLM/embedding calls during ingestion
            "max_task_attempts": 3,     # mark a task failed after this many attempts
        }
        self.load()

    def _load_db_setting(self, db_conn, key: str, decrypt: bool = False) -> str:
        """Helper to directly read settings from SQLite to avoid circular imports."""
        try:
            # Check if settings table exists
            cursor = db_conn.cursor()
            table_check = cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='settings'"
            ).fetchone()
            if not table_check:
                return ""
            row = cursor.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            if not row:
                return ""
            val = row[0]
            if decrypt and val:
                from src.crypto_utils import decrypt_val
                return decrypt_val(val)
            return val
        except Exception:
            return ""

    def load(self):
        # 1. Load from config.json if it exists
        if self.config_path.exists():
            try:
                with open(self.config_path, "r") as f:
                    user_settings = json.load(f)
                    self.settings.update(user_settings)
            except Exception as e:
                print(f"Warning: Failed to load config.json: {e}")

        # 2. Resolve db_path and ensure its directory exists
        raw_db_path = self.settings.get("db_path", "~/.pdfspecs/data.db")
        if raw_db_path.startswith("~"):
            resolved_db_path = Path.home() / raw_db_path.replace("~/", "")
        else:
            resolved_db_path = Path(raw_db_path)

        self.db_path = resolved_db_path.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # 3. Read settings from SQLite if database exists (overriding config.json)
        db_settings = {}
        if self.db_path.exists():
            try:
                # Open direct connection to avoid importing src.db (circular import)
                conn = sqlite3.connect(self.db_path)

                str_keys = ["provider", "llm_model", "embedding_model", "ocr_mode"]
                for key in str_keys:
                    val = self._load_db_setting(conn, key)
                    if val:
                        db_settings[key] = val

                for key in SECRET_KEYS:
                    val = self._load_db_setting(conn, key, decrypt=True)
                    if val:
                        db_settings[key] = val

                int_keys = [
                    "chunk_size", "chunk_overlap", "ocr_min_chars",
                    "max_upload_mb", "enrich_concurrency", "max_task_attempts",
                ]
                for key in int_keys:
                    val = self._load_db_setting(conn, key)
                    if val:
                        try:
                            db_settings[key] = int(val)
                        except (TypeError, ValueError):
                            pass

                # Legacy: map old boolean enable_ocr -> ocr_mode
                legacy_ocr = self._load_db_setting(conn, "enable_ocr")
                if legacy_ocr and "ocr_mode" not in db_settings:
                    db_settings["ocr_mode"] = "always" if str(legacy_ocr).lower() == "true" else "never"

                conn.close()
            except Exception:
                pass  # db not initialized or locked

        # Merge SQLite overrides
        self.settings.update(db_settings)

        # 4. Environment variable overrides (highest precedence)
        self.provider = os.getenv("PDFSPECS_PROVIDER", self.settings.get("provider", "gemini"))
        self.gemini_api_key = os.getenv("GEMINI_API_KEY", self.settings.get("gemini_api_key", ""))
        self.nvidia_api_key = os.getenv("NVIDIA_API_KEY", self.settings.get("nvidia_api_key", ""))
        self.llm_model = os.getenv("PDFSPECS_LLM_MODEL", self.settings.get("llm_model", "gemini-2.5-flash"))
        self.embedding_model = os.getenv("PDFSPECS_EMBEDDING_MODEL", self.settings.get("embedding_model", "text-embedding-004"))
        self.chunk_size = int(os.getenv("PDFSPECS_CHUNK_SIZE", self.settings.get("chunk_size", 2500)))
        self.chunk_overlap = int(os.getenv("PDFSPECS_CHUNK_OVERLAP", self.settings.get("chunk_overlap", 250)))

        ocr_mode = os.getenv("PDFSPECS_OCR_MODE", self.settings.get("ocr_mode", "auto")).lower()
        self.ocr_mode = ocr_mode if ocr_mode in ("auto", "always", "never") else "auto"
        self.ocr_min_chars = int(os.getenv("PDFSPECS_OCR_MIN_CHARS", self.settings.get("ocr_min_chars", 40)))
        self.max_upload_mb = int(os.getenv("PDFSPECS_MAX_UPLOAD_MB", self.settings.get("max_upload_mb", 50)))
        self.enrich_concurrency = max(1, int(os.getenv("PDFSPECS_ENRICH_CONCURRENCY", self.settings.get("enrich_concurrency", 4))))
        self.max_task_attempts = max(1, int(os.getenv("PDFSPECS_MAX_TASK_ATTEMPTS", self.settings.get("max_task_attempts", 3))))

    def save(self):
        """Persist NON-SECRET settings to config.json.

        API keys are intentionally never written here; they live only in the
        encrypted SQLite `settings` table (see src/db.save_setting(encrypt=True)).
        """
        db_path_str = (
            str(self.db_path).replace(str(Path.home()), "~")
            if str(self.db_path).startswith(str(Path.home()))
            else str(self.db_path)
        )
        on_disk = {
            "db_path": db_path_str,
            "provider": self.provider,
            "llm_model": self.llm_model,
            "embedding_model": self.embedding_model,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "ocr_mode": self.ocr_mode,
            "ocr_min_chars": self.ocr_min_chars,
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
