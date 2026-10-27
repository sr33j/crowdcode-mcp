from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_MCP_ALLOWED_HOSTS = (
    "127.0.0.1:*",
    "localhost:*",
    "crowdcode-backend.onrender.com",
    "crowdcode.app",
    "www.crowdcode.app",
)
DEFAULT_MCP_ALLOWED_ORIGINS = (
    "http://127.0.0.1:*",
    "http://localhost:*",
    "https://crowdcode.app",
    "https://www.crowdcode.app",
)


@dataclass(frozen=True)
class Settings:
    database_url: str
    mcp_transport: str = "stdio"
    reviewer_salt: str = "crowdcode-dev"
    host: str = "127.0.0.1"
    port: int = 8000
    mcp_allowed_hosts: tuple[str, ...] = DEFAULT_MCP_ALLOWED_HOSTS
    mcp_allowed_origins: tuple[str, ...] = DEFAULT_MCP_ALLOWED_ORIGINS
    cors_origins: tuple[str, ...] = ("http://127.0.0.1:5173", "http://localhost:5173")
    requests_table: str = "service_requests"
    project_ideas_cache_seconds: int = 86400
    # Rolling-24h request throttle keyed on wallet identity; 0 disables.
    request_rate_limit_per_day: int = 5
    openai_api_key: str | None = None
    openai_model: str = "gpt-5-mini"
    # The service-proposal job asks a product question (would anyone pay for
    # this?) over every request at once, so it gets a more capable model.
    proposal_model: str = "gpt-5.5"
    openai_base_url: str = "https://api.openai.com/v1"
    # Owner references pinned at trust 1.0 (docs/SCORING.md §3.5): the root of
    # trust. Synced into review_owners at startup and at each cron run.
    # Without at least one, no review is ever weighted.
    seed_owners: tuple[str, ...] = ()
    # Smallest payment a weighted review may cite, in USDC base units. Dust
    # payments buy a vote for nothing; 1000 = $0.001.
    min_weighted_amount_atomic: int = 1000
    # Bearer token for GET /api/knowledge/evidence (full review evidence export
    # consumed by the OpenCrowd knowledge-tree generator). Unset disables it.
    knowledge_export_token: str | None = None


def _csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    return _csv(os.environ.get(name, ",".join(default)))


def get_mcp_host() -> str:
    load_dotenv()
    return os.environ.get("HOST", "127.0.0.1").strip() or "127.0.0.1"


def get_mcp_port() -> int:
    load_dotenv()
    return int(os.environ.get("PORT", "8000"))


def get_mcp_allowed_hosts() -> tuple[str, ...]:
    load_dotenv()
    return _env_csv("MCP_ALLOWED_HOSTS", DEFAULT_MCP_ALLOWED_HOSTS)


def get_mcp_allowed_origins() -> tuple[str, ...]:
    load_dotenv()
    return _env_csv("MCP_ALLOWED_ORIGINS", DEFAULT_MCP_ALLOWED_ORIGINS)


def get_settings() -> Settings:
    load_dotenv()

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")

    return Settings(
        database_url=database_url,
        mcp_transport=os.environ.get("MCP_TRANSPORT", "stdio").strip() or "stdio",
        reviewer_salt=os.environ.get("CROWDCODE_REVIEWER_SALT", "crowdcode-dev"),
        host=get_mcp_host(),
        port=get_mcp_port(),
        mcp_allowed_hosts=get_mcp_allowed_hosts(),
        mcp_allowed_origins=get_mcp_allowed_origins(),
        cors_origins=_csv(
            os.environ.get(
                "CORS_ORIGINS",
                "http://127.0.0.1:5173,http://localhost:5173",
            )
        ),
        requests_table=os.environ.get("CROWDCODE_REQUESTS_TABLE", "service_requests").strip()
        or "service_requests",
        project_ideas_cache_seconds=int(
            os.environ.get("PROJECT_IDEAS_CACHE_SECONDS", "86400")
        ),
        request_rate_limit_per_day=int(
            os.environ.get("CROWDCODE_REQUEST_LIMIT_PER_DAY", "5")
        ),
        openai_api_key=os.environ.get("OPENAI_API_KEY", "").strip() or None,
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-5-mini").strip()
        or "gpt-5-mini",
        proposal_model=os.environ.get("CROWDCODE_PROPOSAL_MODEL", "").strip() or "gpt-5.5",
        openai_base_url=os.environ.get(
            "OPENAI_BASE_URL",
            "https://api.openai.com/v1",
        ).strip()
        or "https://api.openai.com/v1",
        seed_owners=tuple(
            owner.lower()
            for owner in _csv(os.environ.get("CROWDCODE_SEED_OWNERS", ""))
        ),
        min_weighted_amount_atomic=int(
            os.environ.get("CROWDCODE_MIN_WEIGHTED_AMOUNT_ATOMIC", "1000")
        ),
        knowledge_export_token=os.environ.get("CROWDCODE_KNOWLEDGE_EXPORT_TOKEN", "").strip()
        or None,
    )
