import os
from pathlib import Path
from cryptography.fernet import Fernet

KEY_PATH = Path.home() / ".pdfspecs" / "secret.key"

_fernet_instance = None

def init_crypto():
    """Initializes the Fernet encryption instance.

    Precedence:
      1. PDFSPECS_FERNET_KEY env var — REQUIRED for hosted/multi-tenant deployments,
         where the container filesystem is ephemeral (a per-deploy random file key would
         make previously-encrypted org keys undecryptable).
      2. Local key file (~/.pdfspecs/secret.key) — convenience for local single-user dev.
    """
    global _fernet_instance
    if _fernet_instance is not None:
        return

    env_key = os.getenv("PDFSPECS_FERNET_KEY")
    if env_key:
        _fernet_instance = Fernet(env_key.encode("utf-8"))
        return

    # Ensure app directory exists
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not KEY_PATH.exists():
        # Generate new random 32-byte key
        key = Fernet.generate_key()
        with open(KEY_PATH, "wb") as f:
            f.write(key)
        # Restrict permissions to owner read/write (0600)
        try:
            os.chmod(KEY_PATH, 0o600)
        except Exception:
            pass # Windows compatibility fallback
    else:
        with open(KEY_PATH, "rb") as f:
            key = f.read()

    _fernet_instance = Fernet(key)

def encrypt_val(val: str) -> str:
    """Encrypts a plaintext string to an AES ciphertext string."""
    if not val:
        return ""
    init_crypto()
    return _fernet_instance.encrypt(val.encode("utf-8")).decode("utf-8")

def decrypt_val(encrypted_val: str) -> str:
    """Decrypts an AES ciphertext string to a plaintext string."""
    if not encrypted_val:
        return ""
    init_crypto()
    try:
        return _fernet_instance.decrypt(encrypted_val.encode("utf-8")).decode("utf-8")
    except Exception:
        # Returns empty if decryption fails (e.g. key mismatch)
        return ""
