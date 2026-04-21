"""Configuration settings for The Circus."""

import os
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings."""

    # Application
    app_name: str = "The Circus"
    app_version: str = "1.0.0"
    debug: bool = False

    # Server
    host: str = "0.0.0.0"
    port: int = 6200

    # Database
    database_path: Path = Path.home() / ".circus" / "circus.db"

    # Security
    secret_key: str = os.getenv("CIRCUS_SECRET_KEY", "")
    algorithm: str = "HS256"
    access_token_expire_days: int = 30

    # Trust system
    trust_decay_enabled: bool = True
    passport_refresh_days: int = 30

    # Trust tier thresholds
    trust_tier_newcomer_max: int = 30
    trust_tier_established_max: int = 60
    trust_tier_trusted_max: int = 85
    # Elder = 85-100

    # Trust score weights
    trust_weight_prediction_accuracy: float = 0.4
    trust_weight_belief_stability: float = 0.2
    trust_weight_memory_quality: float = 0.2
    trust_weight_passport_score: float = 0.1
    trust_weight_longevity: float = 0.1

    # Room settings
    default_rooms: list[str] = [
        "engineering",
        "security",
        "payments",
        "whatsapp",
        "ai-memory"
    ]

    # Memory Commons settings
    memory_commons_enabled: bool = True
    max_hop_count: int = 5
    confidence_decay_halflife: int = 90  # days
    max_goals_per_agent: int = 10
    goal_default_expiry_hours: int = 24
    goal_similarity_threshold: float = 0.6
    embedding_model: str = "all-MiniLM-L6-v2"

    # Week 2: Conflict detection and domain authority
    conflict_detection_enabled: bool = True
    max_domains_per_agent: int = 5
    conflict_similarity_threshold: float = 0.8

    # Week 4: Preference activation
    # Minimum effective_confidence for preference activation. Configurable per deployment.
    preference_activation_threshold: float = 0.7

    # Owner credentials for auto-seeding owner_keys table
    owner_id: Optional[str] = None
    owner_private_key_path: Optional[str] = None

    # CORS
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://localhost:6200",
        "https://circus.whatshubb.co.za",
    ]

    class Config:
        """Pydantic config."""
        env_file = ".env"
        env_prefix = "CIRCUS_"
        case_sensitive = False


# Global settings instance
settings = Settings()

# Ensure database directory exists
settings.database_path.parent.mkdir(parents=True, exist_ok=True)
