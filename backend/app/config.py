from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AI Research Agent"
    environment: Literal["development", "production", "test"] = "development"

    # LLM
    gemini_api_key: SecretStr
    gemini_model: str = Field(min_length=1)
    llm_temperature: float = Field(default=0.2, ge=0, le=2)
    llm_max_output_tokens: int = Field(default=8192, ge=64, le=65536)
    # Tokens the model may spend thinking (Gemini 2.5+). They count against the output limit.
    # 0 turns thinking off, -1 lets the model decide, unset leaves the model's default.
    llm_thinking_budget: int | None = Field(default=1024, ge=-1, le=32768)
    llm_timeout_seconds: float = Field(default=60, gt=0, le=600)
    llm_max_retries: int = Field(default=3, ge=1, le=10)
    llm_validation_retries: int = Field(default=2, ge=0, le=5)

    # Web search
    search_provider: Literal["tavily"] = "tavily"
    tavily_api_key: SecretStr
    search_results_per_query: int = Field(default=5, ge=1, le=20)

    # Web reader
    reader_max_content_chars: int = Field(default=20000, ge=500, le=200000)
    reader_max_download_bytes: int = Field(default=3_000_000, ge=10_000, le=20_000_000)
    reader_max_redirects: int = Field(default=5, ge=0, le=10)
    reader_concurrency: int = Field(default=5, ge=1, le=20)
    reader_user_agent: str = "AIResearchAgent/0.1 (+research bot)"

    # PDF export: optional TTF fonts with wider character coverage than the bundled Vera
    pdf_font_path: str | None = None
    pdf_bold_font_path: str | None = None

    # Storage
    database_url: str = "sqlite+aiosqlite:///./research.db"
    # Production runs `alembic upgrade head` as its own step before the app starts.
    run_migrations_on_startup: bool = True

    # Background jobs: "inprocess" runs them inside the API (local development);
    # "arq" queues them in Redis for a separate worker process, so they survive restarts.
    job_backend: Literal["inprocess", "arq"] = "inprocess"
    redis_url: str | None = None
    job_max_tries: int = Field(default=3, ge=1, le=10)

    # Accounts and sessions
    session_days: int = Field(default=14, ge=1, le=90)
    cookie_secure: bool | None = None  # None: secure cookies in production only
    login_attempts_per_15_minutes: int = Field(default=10, ge=1, le=1000)

    # API surface
    enable_docs: bool | None = None  # None: /api/docs on, except in production
    log_format: Literal["text", "json"] = "text"

    # Research limits
    max_research_rounds: int = Field(default=3, ge=1, le=10)
    max_searches_per_round: int = Field(default=3, ge=1, le=10)
    max_pages_per_research: int = Field(default=10, ge=1, le=50)
    max_pages_per_round: int = Field(default=5, ge=1, le=20)
    max_total_searches: int = Field(default=9, ge=1, le=50)
    max_llm_calls: int = Field(default=30, ge=3, le=200)
    max_research_seconds: int = Field(default=300, ge=30, le=3600)
    max_total_tokens: int = Field(default=500_000, ge=10_000, le=10_000_000)

    # Abuse and cost control for starting jobs
    research_jobs_per_minute: int = Field(default=5, ge=1, le=1000)
    research_jobs_per_hour: int = Field(default=30, ge=1, le=10000)
    max_active_jobs: int = Field(default=10, ge=1, le=1000)  # pending + running, across all users
    max_active_jobs_per_user: int = Field(default=3, ge=1, le=100)
    llm_concurrency: int = Field(default=3, ge=1, le=10)
    # Research jobs running at once in this server process; the rest wait as "pending".
    max_concurrent_jobs: int = Field(default=2, ge=1, le=10)
    request_timeout_seconds: float = Field(default=20, gt=0, le=120)

    # CORS is only needed when the frontend is served from another origin. In development Vite
    # proxies /api, and in production Nginx serves both, so the browser sees a single origin.
    # Set as a JSON list in .env, e.g. CORS_ORIGINS=["http://localhost:5173"]
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


    @field_validator("gemini_api_key", "tavily_api_key")
    @classmethod
    def key_must_not_be_blank(cls, value: SecretStr, info) -> SecretStr:
        # An empty KEY= line in .env would otherwise pass and fail later with a confusing SDK error.
        if not value.get_secret_value().strip():
            raise ValueError(f"{info.field_name.upper()} is empty. Set it in backend/.env")
        return value


    @model_validator(mode="after")
    def production_requirements(self) -> "Settings":
        """Refuse to start in production with settings that only suit local development."""
        if self.environment != "production":
            return self
        problems = []
        if self.database_url.startswith("sqlite"):
            problems.append("DATABASE_URL must point at PostgreSQL, not SQLite")
        if self.job_backend != "arq":
            problems.append("JOB_BACKEND must be 'arq' so jobs survive restarts")
        if not self.redis_url:
            problems.append("REDIS_URL is required for the job queue and shared rate limits")
        if self.cookie_secure is False:
            problems.append("COOKIE_SECURE must not be false (sessions need HTTPS)")
        if problems:
            raise ValueError("Unsafe production configuration: " + "; ".join(problems))
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def secure_cookies(self) -> bool:
        return self.is_production if self.cookie_secure is None else self.cookie_secure

    @property
    def docs_enabled(self) -> bool:
        return (not self.is_production) if self.enable_docs is None else self.enable_docs


@lru_cache
def get_settings() -> Settings:
    return Settings()
