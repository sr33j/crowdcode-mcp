from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    database_url: str
    mcp_transport: str = "stdio"
    reviewer_salt: str = "crowdcode-dev"


def get_settings() -> Settings:
    load_dotenv()

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")

    return Settings(
        database_url=database_url,
        mcp_transport=os.environ.get("MCP_TRANSPORT", "stdio").strip() or "stdio",
        reviewer_salt=os.environ.get("CROWDCODE_REVIEWER_SALT", "crowdcode-dev"),
    )
