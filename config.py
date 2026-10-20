"""
config.py — Centralized settings, loaded from environment variables.

Today: values come from a local .env file (python-dotenv).
Production upgrade path: replace load_dotenv() with a call to your vault
client (HashiCorp Vault, AWS Secrets Manager, GCP Secret Manager, etc.) that
populates os.environ before Settings() is constructed.

Nothing else in the codebase needs to change because other modules read
secrets through `settings`.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv


# Load variables from .env
load_dotenv()


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable safely."""
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:

    # ---------------------------------------------------------
    # Secrets
    # ---------------------------------------------------------

    jwt_secret: str = field(
        default_factory=lambda: os.getenv(
            "JWT_SECRET",
            "dev-insecure-secret-change-me"
        )
    )

    # Google Gemini API key
    gemini_api_key: str = field(
        default_factory=lambda: os.getenv("GOOGLE_API_KEY", "")
    )

    # ---------------------------------------------------------
    # Auth
    # ---------------------------------------------------------

    jwt_algorithm: str = "HS256"

    jwt_expiry_minutes: int = field(
        default_factory=lambda: _env_int(
            "JWT_EXPIRY_MINUTES",
            60
        )
    )

    # ---------------------------------------------------------
    # Persistence
    # ---------------------------------------------------------

    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL",
            "sqlite:///./agent_state.db"
        )
    )

    # ---------------------------------------------------------
    # Rate limiting
    # ---------------------------------------------------------

    redis_url: str = field(
        default_factory=lambda: os.getenv(
            "REDIS_URL",
            "redis://localhost:6379/0"
        )
    )

    rate_limit_requests: int = field(
        default_factory=lambda: _env_int(
            "RATE_LIMIT_REQUESTS",
            10
        )
    )

    rate_limit_window_seconds: int = field(
        default_factory=lambda: _env_int(
            "RATE_LIMIT_WINDOW_SECONDS",
            60
        )
    )

    # ---------------------------------------------------------
    # Agent / LLM
    # ---------------------------------------------------------

    gemini_model: str = field(
        default_factory=lambda: os.getenv(
            "GEMINI_MODEL",
            "gemini-3.6-flash"
        )
    )


# Create the settings object used by the application
settings = Settings()


# Warn if JWT secret is still using the development default
if settings.jwt_secret == "dev-insecure-secret-change-me":
    import warnings

    warnings.warn(
        "JWT_SECRET is not set — using an insecure default. "
        "Set JWT_SECRET in your .env before doing anything real.",
        stacklevel=2,
    )