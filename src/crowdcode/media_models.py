"""Which named models an offering sells, for the capability tree (C9).

W1's index stores them: `index_offering_models` (one row per offering and
model, from the seller's `/models` catalog, its route or its description) and
`model_quality_priors` (each model's leaderboard prior on the judge's 1–5
scale, with its source and date). Names are normalized by
`crowdcode.index_models`, the one table of model patterns.

Until the index's first model sweep has filled `index_offering_models`, the
tree reads models from the same text with W1's `find_models`, so both give
the same ids. Once the table has rows it is the only source: an offering it
gives no model sells none the tree should list.
"""

from __future__ import annotations

import re
from typing import Any

from crowdcode import index_models

# A node's media modalities: audio covers speech and music.
NODE_MODALITIES = {"video": ("video",), "image": ("image",), "audio": ("audio", "music"), "llm": ("llm",)}

_WORDS = {"gpt": "GPT", "tts": "TTS", "hd": "HD", "ai": "AI", "xl": "XL", "sdxl": "SDXL", "ltx": "LTX",
          "elevenlabs": "ElevenLabs", "minimax": "MiniMax", "openai": "OpenAI", "pixverse": "PixVerse",
          "heygen": "HeyGen", "happyhorse": "HappyHorse"}


def label(model_id: str) -> str:
    """`veo-3.1-fast` -> `Veo 3.1 Fast`, `kling-v3-pro` -> `Kling v3 Pro`."""
    parts = []
    for token in model_id.split("-"):
        if re.fullmatch(r"v?\d+(\.\d+)?|o\d+", token):
            parts.append(token)
        else:
            parts.append(_WORDS.get(token, token.capitalize()))
    return " ".join(parts)


def _schema_models(schema: Any) -> str:
    """The values of `model` fields in an input schema: enums, defaults, examples."""
    found: list[str] = []

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if "model" in str(k).lower() and isinstance(v, dict):
                    found.extend(str(x) for x in (v.get("enum") or []) + [v.get("default"), v.get("example")] if x)
                walk(v, str(k))
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif isinstance(value, str) and "model" in key.lower():
            found.append(value)

    walk(schema)
    return " ".join(found)[:4000]


def detect(row: dict[str, Any]) -> list[dict[str, str]]:
    """W1's patterns over the offering's own words, before the index has stored any."""
    route = re.sub(r"^https?://[^/]+", "", str(row.get("route_template") or ""))
    text = " ".join(str(t) for t in (route, row.get("service_label"), row.get("service_name"),
                                     row.get("description"), _schema_models(row.get("input_schema"))) if t)
    return [{"model": m.model, "family": m.family, "modality": m.modality} for m in index_models.find_models(text)]


def load(conn: Any) -> tuple[dict[int, list[dict[str, Any]]] | None, dict[str, dict[str, Any]]]:
    """(models by index row id, or None before the first model sweep; priors by model)."""
    try:
        rows = conn.execute(
            "select resource_id, model, model_family as family, modality, usd_per_second, pricing from index_offering_models"
        ).fetchall()
        priors = {r["model"]: dict(r) for r in conn.execute(
            "select model, model_family, modality, source, source_url, as_of, rank, prior_rating, leaderboard_name "
            "from model_quality_priors").fetchall()}
    except Exception:  # the tables are W1's; a tree without them still builds
        conn.rollback()
        return None, {}
    if not rows:
        return None, priors
    by_resource: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_resource.setdefault(int(row["resource_id"]), []).append(dict(row))
    return by_resource, priors


def models_for(row: dict[str, Any], stored: dict[int, list[dict[str, Any]]] | None,
               modalities: tuple[str, ...] | None = None) -> list[dict[str, str]]:
    """[{model, family, modality}] an offering sells, limited to `modalities`."""
    found = stored.get(int(row["id"]), []) if stored is not None else detect(row)
    return [m for m in found if modalities is None or m["modality"] in modalities]
