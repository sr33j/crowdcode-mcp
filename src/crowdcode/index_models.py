"""Which model an offering sells, and how good that model is said to be (SRI-115 C9).

An *offering* is one paid endpoint of one seller (an ``index_resources`` row).
When it sells a named model, the index records a normalized ``model`` (for
example ``veo-3.1``, ``kling-v3-pro``, ``seedance-2.5``, ``sora-2``,
``wan-2.6``, ``flux-1.1-pro``, ``gpt-6.2-astra``), its ``model_family`` and
its ``modality`` (video, image, audio, music, llm). Several sellers of one
model then compete on price and delivery; different models compete on
quality.

Where the model comes from, most specific first:

1. **the seller's own model catalog** (``/models``): one endpoint, many
   models (OpenRouter, Kling and HeyGen ``/v1/videos``, BlockRun). Every
   model it lists is a row of ``index_offering_models``, with the seller's
   own id and pricing;
2. **the route** (``/api/generate/veo-3.1/generate``,
   ``/fal-ai/kling-video/v3/pro/text-to-video``);
3. **the description**, when it names exactly one model.

Names are normalized by one table of patterns (``_PATTERNS``) so that
``google/veo-3.1``, ``fal-ai/veo3.1`` and "Veo 3.1" are the same model.
Versions drop a trailing ``.0`` (``kling-v3.0-pro`` is ``kling-v3-pro``,
``seedance-2.0`` is ``seedance-2``). Leaderboard names go through the same
table, so ``model_quality_priors`` joins on ``model``.

Quality priors come from Artificial Analysis's public leaderboards (video,
image, speech: arena Elo; language models: the Intelligence Index), read
weekly. Each row keeps its source URL and date. ``prior_rating`` maps a
model's standing on its board to the judge's 1–5 scale, between 3 and 5: a
model ranked on a public arena is a competent model (3.0 is neutral, the same
as an unknown offering); a 1 means a failed purchase, which a leaderboard
cannot say.

- Arena boards (Elo): ``5 − min(2, gap / 150)``, ``gap`` being the Elo
  distance to the board's leader. 150 Elo is about a 70% head-to-head
  preference, so the leader is 5.0 and a model 300 or more behind is 3.0.
- The language-model board (Intelligence Index, not Elo): ``3 + 2 × percentile``.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Iterable

import httpx

MODALITIES = ("video", "image", "audio", "music", "llm", "embedding")


@dataclass(frozen=True)
class Model:
    model: str
    family: str
    modality: str


def _v(version: str | None) -> str | None:
    """`3.0` -> `3`, `2.50` -> `2.5`; None stays None."""
    if not version:
        return None
    version = version.lstrip("v")
    if "." in version:
        version = version.rstrip("0").rstrip(".") or "0"
    return version


def _join(*parts: str | None) -> str:
    return "-".join(p for p in parts if p)


# (regex, family, modality, builder). The text is lowercased and every run of
# spaces, slashes, underscores, colons and brackets is one "-", and resolution
# tokens (720p, 4k) are removed. Order matters: specific before general.
_Builder = Callable[[re.Match[str]], str]
_PATTERNS: list[tuple[re.Pattern[str], str, str, _Builder]] = []


def _p(pattern: str, family: str, modality: str, build: _Builder) -> None:
    _PATTERNS.append((re.compile(pattern), family, modality, build))


_VER = r"(\d+(?:\.\d+)?)"

# --- video ---
_p(rf"\bveo-?{_VER}(?:-(fast|lite))?", "veo", "video", lambda m: _join("veo", _v(m[1]), m[2]))
_p(rf"\bsora-?(\d+)(?:-(pro))?", "sora", "video", lambda m: _join("sora", m[1], m[2]))
_p(rf"\bseedance(?:-{_VER})?(?:-(pro|lite|fast|mini|flash))?", "seedance", "video",
   lambda m: _join("seedance", _v(m[1]), m[2]))
_p(r"\bkling(?:-video)?-(?:(o)(\d+)|v?(\d+(?:\.\d+)?))(?:-(omni))?(?:-(pro|std|standard|master))?", "kling", "video",
   lambda m: _join("kling", f"o{m[2]}" if m[1] else f"v{_v(m[3])}", m[4],
                   {"standard": "std"}.get(m[5] or "", m[5])))
_p(rf"\bwan-?{_VER}(?:-(prime|turbo|plus|fast|flash))?", "wan", "video", lambda m: _join("wan", _v(m[1]), m[2]))
_p(rf"\b(?:hailuo|minimax-h)-?{_VER}(?:-(max|fast|pro))?", "hailuo", "video",
   lambda m: _join("hailuo", _v(m[1]), m[2]))
_p(r"\bminimax-video-(\d+)(?:-(live))?", "hailuo", "video", lambda m: _join("minimax-video", m[1], m[2]))
_p(rf"\bgrok-(?:imagine-)?video(?:-{_VER})?(?:-(lite))?", "grok-imagine", "video",
   lambda m: _join("grok-imagine-video", _v(m[1]), m[2]))
_p(rf"\brunway-gen-{_VER}(?:-(turbo))?", "runway", "video", lambda m: _join("runway-gen", _v(m[1]), m[2]))
_p(rf"\brunway-aleph(?:-{_VER})?", "runway", "video", lambda m: _join("runway-aleph", _v(m[1])))
_p(r"\bheygen-avatar-(\d+|iv)(?:-(turbo))?", "heygen", "video", lambda m: _join("heygen-avatar", m[1], m[2]))
_p(r"\bavatar-iv\b", "heygen", "video", lambda m: "heygen-avatar-iv")
_p(r"\bheygen-video-(\d+)", "heygen", "video", lambda m: _join("heygen-video", m[1]))
_p(r"\bflux-(\d+)-video", "flux", "video", lambda m: _join("flux", m[1], "video"))
_p(r"\bflux-video-(edit|upscale)", "flux", "video", lambda m: _join("flux-video", m[1]))
_p(rf"\bgemini-omni-{_VER}(?:-(flash|pro))?", "gemini-omni", "video", lambda m: _join("gemini-omni", _v(m[1]), m[2]))
_p(rf"\bhappyhorse-{_VER}", "happyhorse", "video", lambda m: _join("happyhorse", _v(m[1])))
_p(rf"\bpixverse-v?{_VER}", "pixverse", "video", lambda m: _join("pixverse", _v(m[1])))
_p(rf"\bluma-ray-?{_VER}?", "luma", "video", lambda m: _join("luma-ray", _v(m[1])))
_p(rf"\bltx(?:-video)?-?{_VER}?", "ltx", "video", lambda m: _join("ltx-video", _v(m[1])))
_p(rf"\bhunyuan-?video(?:-{_VER})?", "hunyuan", "video", lambda m: _join("hunyuan-video", _v(m[1])))
_p(rf"\bskyreels-v?{_VER}", "skyreels", "video", lambda m: _join("skyreels", _v(m[1])))
_p(rf"\bvidu-?(q?\d+(?:\.\d+)?)", "vidu", "video", lambda m: _join("vidu", m[1]))
# --- image ---
_p(rf"\bgpt-image-{_VER}(?:-(mini|sunburst|flare))?", "gpt-image", "image", lambda m: _join("gpt-image", _v(m[1]), m[2]))
_p(rf"\bnano-banana(?:-{_VER})?(?:-(pro|lite))?", "nano-banana", "image", lambda m: _join("nano-banana", _v(m[1]), m[2]))
_p(rf"\bflux-?{_VER}?-(pro|dev|schnell|max|kontext|ultra|flex)(?:-v?{_VER})?", "flux", "image",
   lambda m: _join("flux", _v(m[1] or m[3]), m[2]))
_p(rf"\bflux-?{_VER}\b", "flux", "image", lambda m: _join("flux", _v(m[1])))
_p(rf"\bgrok-(?:imagine-)?image(?:-{_VER})?(?:-(pro))?", "grok-imagine", "image",
   lambda m: _join("grok-imagine-image", _v(m[1]), m[2]))
_p(rf"\bseedream-?{_VER}?(?:-(pro|lite))?", "seedream", "image", lambda m: _join("seedream", _v(m[1]), m[2]))
_p(rf"\bimagen-?{_VER}(?:-(ultra|fast))?", "imagen", "image", lambda m: _join("imagen", _v(m[1]), m[2]))
_p(rf"\bideogram(?:-v)?-?{_VER}?", "ideogram", "image", lambda m: _join("ideogram", _v(m[1])))
_p(rf"\brecraft-v?{_VER}", "recraft", "image", lambda m: _join("recraft", _v(m[1])))
_p(rf"\bmidjourney-v?{_VER}?", "midjourney", "image", lambda m: _join("midjourney", _v(m[1])))
_p(rf"\bqwen-image(?:-{_VER})?", "qwen-image", "image", lambda m: _join("qwen-image", _v(m[1])))
_p(rf"\bcogview-{_VER}", "cogview", "image", lambda m: _join("cogview", _v(m[1])))
_p(rf"\bmai-image-{_VER}", "mai-image", "image", lambda m: _join("mai-image", _v(m[1])))
_p(rf"\bhidream(?:-i\d)?(?:-(full|dev|fast))?", "hidream", "image", lambda m: _join("hidream", m[1]))
# --- music and audio ---
_p(rf"\blyria(?:-{_VER})?", "lyria", "music", lambda m: _join("lyria", _v(m[1])))
_p(rf"\bsuno(?:-v?{_VER})?", "suno", "music", lambda m: _join("suno", _v(m[1])))
_p(rf"\beleven(?:labs)?-music(?:-v?{_VER})?", "elevenlabs", "music", lambda m: _join("eleven-music", _v(m[1])))
_p(rf"\beleven(?:labs)?-(?:v|turbo-v|flash-v|multilingual-v)?{_VER}(?:-(turbo|flash))?", "elevenlabs", "audio",
   lambda m: _join("eleven-v" + (_v(m[1]) or ""), m[2]))
_p(rf"\bheygen-voice-(\d+)(?:-(turbo))?", "heygen", "audio", lambda m: _join("heygen-voice", m[1], m[2]))
_p(rf"\bvoxtral-(mini-)?tts(?:-{_VER})?", "voxtral", "audio", lambda m: _join("voxtral-tts", _v(m[2])))
_p(rf"\bwhisper(?:-large)?(?:-v?{_VER})?", "whisper", "audio", lambda m: _join("whisper", _v(m[1])))
_p(r"\bgpt-4o-(mini-)?(tts|transcribe)", "gpt-audio", "audio", lambda m: _join("gpt-4o", (m[1] or "").rstrip("-"), m[2]))
# --- language models ---
_p(r"\bgpt-4o(?:-(mini))?", "gpt", "llm", lambda m: _join("gpt-4o", m[1]))
_p(rf"\bgpt-{_VER}(?:-(mini|nano|pro|turbo|astra|sol|terra|luna|codex|chat))?", "gpt", "llm",
   lambda m: _join("gpt", _v(m[1]), m[2]))
_p(rf"\bclaude-(opus|sonnet|haiku|fable)-(\d+(?:[.-]\d+)?)", "claude", "llm",
   lambda m: _join("claude", m[1], _v(m[2].replace("-", "."))))
_p(rf"\bclaude-{_VER}-(opus|sonnet|haiku)", "claude", "llm", lambda m: _join("claude", m[2], _v(m[1])))
_p(rf"\bgemini-{_VER}-(pro|flash-lite|flash)", "gemini", "llm", lambda m: _join("gemini", _v(m[1]), m[2]))
_p(rf"\bllama-?{_VER}(?:-(\d+b))?", "llama", "llm", lambda m: _join("llama", _v(m[1]), m[2]))
_p(r"\bdeepseek-(v\d+(?:\.\d+)?|r\d|chat|reasoner)", "deepseek", "llm", lambda m: _join("deepseek", m[1]))
_p(rf"\bqwen-?{_VER}(?:-(max|plus|turbo|coder))?", "qwen", "llm", lambda m: _join("qwen", _v(m[1]), m[2]))
_p(rf"\bgrok-{_VER}(?:-(mini|fast|code|heavy))?", "grok", "llm", lambda m: _join("grok", _v(m[1]), m[2]))
_p(rf"\bkimi-k{_VER}", "kimi", "llm", lambda m: _join("kimi-k" + (_v(m[1]) or "")))
_p(rf"\bglm-{_VER}", "glm", "llm", lambda m: _join("glm", _v(m[1])))
_p(rf"\b(mistral|mixtral|magistral|devstral|codestral)-(large|medium|small)?-?{_VER}?", "mistral", "llm",
   lambda m: _join(m[1], m[2], _v(m[3])))
_p(r"\bvenice-uncensored(?:-(\d+(?:[.-]\d+)?))?", "venice", "llm", lambda m: _join("venice-uncensored", (m[1] or "").replace("-", ".") or None))


def _text(value: str) -> str:
    text = html.unescape(value).lower()
    text = re.sub(r"\b\d{3,4}p\b|\b[248]k\b", " ", text)
    text = re.sub(r"[\s/_:()\[\],|+]+", "-", text)
    return re.sub(r"-{2,}", "-", text).strip("-")


def find_models(value: str | None) -> list[Model]:
    """Every distinct model named in a piece of text, in order of appearance."""
    if not value:
        return []
    text = _text(value)
    found: list[tuple[int, Model]] = []
    taken: list[tuple[int, int]] = []
    for pattern, family, modality, build in _PATTERNS:
        for match in pattern.finditer(text):
            if any(a < match.end() and match.start() < b for a, b in taken):
                continue
            taken.append((match.start(), match.end()))
            found.append((match.start(), Model(build(match), family, modality)))
    seen: dict[str, Model] = {}
    for _, model in sorted(found, key=lambda pair: pair[0]):
        seen.setdefault(model.model, model)
    return list(seen.values())


def normalize_model(value: str | None) -> Model | None:
    """The one model a name refers to (a seller's id, a leaderboard name), or None."""
    models = find_models(value)
    return models[0] if models else None


def offering_model(route_path: str, description: str | None, service_name: str | None) -> tuple[Model, str] | None:
    """The single model an endpoint sells, judged from its route, else its description."""
    in_route = find_models(route_path)
    if len(in_route) == 1:
        return in_route[0], "route"
    if in_route:
        return None  # several in one path is a router, not one model
    in_text = find_models(" ".join(p for p in (service_name, description) if p))
    if len(in_text) == 1:
        return in_text[0], "description"
    return None


def _model_fields(value: Any) -> list[dict[str, Any]]:
    """Every `model` property (body or query) of an input schema."""
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        props = value.get("properties")
        if isinstance(props, dict):
            for name, prop in props.items():
                if str(name).lower() in ("model", "model_id", "modelid") and isinstance(prop, dict):
                    found.append(prop)
        for inner in value.values():
            found.extend(_model_fields(inner))
    elif isinstance(value, list):
        for inner in value:
            found.extend(_model_fields(inner))
    return found


def schema_models(schema: Any) -> list[tuple[Model, str, dict[str, Any] | None]]:
    """The models a router endpoint sells, from its own input schema's `model`
    enum: [(model, the seller's id, {"clip_seconds", "clip_usd"} or None)].

    The clip price is the first "<id> <N>s $<price>" the field's description
    gives for that id (Treza: "veo-3.1 4s $2.24, 6s $3.36"); per-model prices
    differ far more than the listing's single price says."""
    out: dict[str, tuple[Model, str, dict[str, Any] | None]] = {}
    for field in _model_fields(schema):
        description = str(field.get("description") or "")
        for value in field.get("enum") or []:
            if not isinstance(value, str):
                continue
            model = normalize_model(value)
            if model is None or model.model in out:
                continue
            price = None
            match = re.search(rf"(?<![\w.-]){re.escape(value)}(?![\w.-])[^;]*?(\d+)\s*s\s*\$(\d+(?:\.\d+)?)", description)
            if match:
                price = {"clip_seconds": int(match[1]), "clip_usd": float(match[2])}
            out[model.model] = (model, value, price)
    return list(out.values())


_MODALITY_WORDS = (
    ("video", r"\b(text|image|reference)-to-video\b|\bvideo-generation\b|\bgenerate-(a-)?videos?\b|\bt2v\b|\bi2v\b"),
    ("image", r"\btext-to-image\b|\bimage-generation\b|\bgenerate-(an-)?images?\b|\bimage-editing\b"),
    ("music", r"\bmusic-generation\b|\bgenerate-music\b|\bsong-generation\b"),
    ("audio", r"\btext-to-speech\b|\bspeech-synthesis\b|\btranscri(be|ption)\b|\bspeech-to-text\b"),
)


def modality_hint(*texts: str | None) -> str | None:
    """A modality from wording alone, for offerings that name no model."""
    text = _text(" ".join(t for t in texts if t))
    for modality, pattern in _MODALITY_WORDS:
        if re.search(pattern, text):
            return modality
    return None


# --- Seller model catalogs ---------------------------------------------------------


@dataclass(frozen=True)
class Catalog:
    """A seller's free list of the models one or more of its paid endpoints sell."""

    url: str
    # Paid endpoint URL prefixes and, per model kind/category, which of them sell it.
    routes: dict[str, tuple[str, ...]]
    default: tuple[str, ...] = ()


# BlockRun is not in mpp.dev, so its catalog is named here. Categories are its own.
STATIC_CATALOGS = (
    Catalog(
        url="https://blockrun.ai/api/v1/models",
        routes={
            "video": ("https://blockrun.ai/api/v1/videos/generations",),
            "image": ("https://blockrun.ai/api/v1/images/generations", "https://blockrun.ai/api/v1/images/image2image"),
            "speech": ("https://blockrun.ai/api/v1/audio/speech",),
            "tts": ("https://blockrun.ai/api/v1/audio/speech",),
            "music": ("https://blockrun.ai/api/v1/audio/generations",),
            "sound_effect": ("https://blockrun.ai/api/v1/audio/sound-effects",),
            "chat": ("https://blockrun.ai/api/v1/chat/completions", "https://blockrun.ai/api/v1/messages"),
        },
    ),
)


def mpp_catalogs(services: Iterable[dict[str, Any]]) -> list[Catalog]:
    """mpp.dev services that publish a free ``…/models`` route next to paid ones.

    ``GET /v1/videos/models`` describes ``POST /v1/videos``; ``GET /v1/audio/models``
    describes ``/v1/audio/speech`` and ``/v1/audio/transcriptions`` by each model's kind.
    """
    catalogs = []
    for service in services:
        if not isinstance(service, dict) or not isinstance(service.get("serviceUrl"), str):
            continue
        base = service["serviceUrl"].rstrip("/")
        endpoints = [e for e in service.get("endpoints") or [] if isinstance(e, dict) and isinstance(e.get("path"), str)]
        paid = [e for e in endpoints if isinstance(e.get("payment"), dict)]
        for endpoint in endpoints:
            path = endpoint["path"]
            if endpoint.get("payment") or str(endpoint.get("method") or "GET").upper() != "GET" or not path.endswith("/models"):
                continue
            prefix = path[: -len("/models")]
            siblings = [base + e["path"] for e in paid if e["path"].startswith(prefix) and ":" not in e["path"]]
            if not siblings:
                continue
            routes = {
                "speech": tuple(u for u in siblings if u.endswith("/speech")),
                "transcription": tuple(u for u in siblings if u.endswith("/transcriptions")),
            }
            catalogs.append(Catalog(url=base + path, routes={k: v for k, v in routes.items() if v},
                                    default=tuple(siblings)))
    return catalogs


def _usd_per_second(pricing: Any) -> float | None:
    """A per-second USD price from a seller's pricing keys, when it states one."""
    if not isinstance(pricing, dict):
        return None
    for key in ("duration_seconds", "duration_seconds_without_audio", "duration_seconds_720p",
                "text_to_video_duration_seconds_720p", "duration_seconds_1080p"):
        if key in pricing:
            try:
                return float(pricing[key])
            except (TypeError, ValueError):
                pass
    for key in ("cents_per_second_output", "cents_per_video_output_second_720p", "cents_per_second_output_720p"):
        if key in pricing:
            try:
                return float(pricing[key]) / 100
            except (TypeError, ValueError):
                pass
    seconds = [float(v) for k, v in pricing.items() if k.startswith("duration_seconds") and _is_number(v)]
    return min(seconds) if seconds else None


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class CatalogEntry:
    endpoint: str  # a paid endpoint URL
    model: Model
    seller_model_id: str
    pricing: Any
    usd_per_second: float | None


def parse_catalog(catalog: Catalog, payload: Any) -> list[CatalogEntry]:
    items = payload.get("data") if isinstance(payload, dict) else payload
    entries = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        if item.get("available") is False:
            continue
        model = normalize_model(item["id"]) or normalize_model(str(item.get("name") or ""))
        if model is None:
            continue
        kinds = [str(k) for k in ([item.get("kind")] + list(item.get("categories") or [])) if k]
        endpoints: list[str] = []
        for kind in kinds:
            endpoints.extend(catalog.routes.get(kind, ()))
        if not endpoints:
            endpoints = list(catalog.default)
        if not endpoints:
            # BlockRun: a language model's categories are chat, reasoning, coding…
            endpoints = list(catalog.routes.get("chat", ())) if model.modality == "llm" else []
        pricing = item.get("pricing_skus") or item.get("pricing") or item.get("price")
        for endpoint in dict.fromkeys(endpoints):
            entries.append(CatalogEntry(endpoint, model, item["id"], pricing, _usd_per_second(pricing)))
    return entries


def fetch_catalogs(client: httpx.Client, catalogs: Iterable[Catalog]) -> tuple[list[CatalogEntry], dict[str, str]]:
    """Every catalog's entries; a catalog that fails is reported, not fatal."""
    entries: list[CatalogEntry] = []
    failures: dict[str, str] = {}
    for catalog in catalogs:
        try:
            response = client.get(catalog.url)
            if response.status_code != 200:
                failures[catalog.url] = f"HTTP {response.status_code}"
                continue
            entries.extend(parse_catalog(catalog, response.json()))
        except (httpx.HTTPError, ValueError) as error:
            failures[catalog.url] = f"{type(error).__name__}"
    return entries, failures


# --- Quality priors ----------------------------------------------------------------

AA = "https://artificialanalysis.ai"
# (board, url, metric). Text-to-video before image-to-video: a model keeps its
# first board's score.
LEADERBOARDS = (
    ("text-to-video", f"{AA}/video/leaderboard/text-to-video", "elo"),
    ("image-to-video", f"{AA}/video/leaderboard/image-to-video", "elo"),
    ("text-to-image", f"{AA}/image/leaderboard/text-to-image", "elo"),
    ("text-to-speech", f"{AA}/text-to-speech/leaderboard", "elo"),
    ("llm-intelligence-index", f"{AA}/leaderboards/models", "intelligence_index"),
)
# A board scores only its own kind of model ("Wan 2.6 Image" on the image
# board is not the video model wan-2.6).
BOARD_MODALITY = {"text-to-video": "video", "image-to-video": "video", "text-to-image": "image",
                  "text-to-speech": "audio", "llm-intelligence-index": "llm"}


# prior_rating is between PRIOR_FLOOR and PRIOR_FLOOR + PRIOR_SPAN (3.0 to 5.0).
PRIOR_FLOOR = 3.0
PRIOR_SPAN = 2.0
# Elo boards: one point of rating per ELO_PER_POINT behind the leader.
ELO_PER_POINT = 150.0


@dataclass(frozen=True)
class Prior:
    model: Model
    board: str
    source_url: str
    metric: str
    score: float
    rank: int
    prior_rating: float
    leaderboard_name: str


def _cells(row: str) -> list[str]:
    return [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", c)).split())
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]


def parse_leaderboard(board: str, source_url: str, metric: str, page: str) -> list[Prior]:
    """The models on one leaderboard page, best first, mapped to normalized ids.

    Arena tables read ``rank | range | model | 1174±13… | …``; the language
    model table reads ``model | index | …``. Several rows can name one model
    (resolutions, tiers): the best score is kept.
    """
    table = re.search(r"<table.*?</table>", page, re.S)
    if not table:
        return []
    scored: list[tuple[float, str]] = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(0), re.S):
        cells = [c for c in _cells(row)]
        if metric == "elo":
            if len(cells) < 4 or not cells[0].isdigit():
                continue
            match = re.match(r"(\d{3,4}(?:\.\d+)?)", cells[3].replace(",", ""))
            name = cells[2]
        else:
            if len(cells) < 2:
                continue
            match = re.match(r"(\d+(?:\.\d+)?)$", cells[1])
            name = cells[0]
        if match and name:
            scored.append((float(match.group(1)), name))
    scored.sort(key=lambda pair: -pair[0])
    best: dict[str, tuple[float, str, Model]] = {}
    for score, name in scored:
        model = normalize_model(name)
        if model is not None and BOARD_MODALITY.get(board, model.modality) != model.modality:
            continue
        if model is not None and model.model not in best:
            best[model.model] = (score, name, model)
    ranked = sorted(best.values(), key=lambda item: -item[0])
    n = len(ranked)
    leader = ranked[0][0] if ranked else 0.0

    def rating(index: int, score: float) -> float:
        if metric == "elo":
            return round(PRIOR_FLOOR + PRIOR_SPAN - min(PRIOR_SPAN, (leader - score) / ELO_PER_POINT), 3)
        return round(PRIOR_FLOOR + PRIOR_SPAN * (1 - index / (n - 1)), 3) if n > 1 else 4.0

    return [
        Prior(model, board, source_url, metric, score, index + 1, rating(index, score), name)
        for index, (score, name, model) in enumerate(ranked)
    ]


def fetch_priors(client: httpx.Client) -> tuple[list[Prior], dict[str, str]]:
    priors: list[Prior] = []
    failures: dict[str, str] = {}
    for board, url, metric in LEADERBOARDS:
        try:
            response = client.get(url, headers={"Accept": "text/html"}, follow_redirects=True)
            if response.status_code != 200:
                failures[board] = f"HTTP {response.status_code}"
                continue
            rows = parse_leaderboard(board, url, metric, response.text)
            if not rows:
                failures[board] = "no table"
                continue
            priors.extend(rows)
        except httpx.HTTPError as error:
            failures[board] = type(error).__name__
    return priors, failures


def priors_rows(priors: Iterable[Prior], as_of: date) -> list[tuple[Any, ...]]:
    """One row per model: its first board (LEADERBOARDS order) wins."""
    rows: dict[str, tuple[Any, ...]] = {}
    for prior in priors:
        if prior.model.model in rows:
            continue
        rows[prior.model.model] = (
            prior.model.model, prior.model.family, prior.model.modality,
            f"artificialanalysis:{prior.board}", prior.source_url, as_of,
            prior.score if prior.metric == "elo" else None, prior.metric, prior.score,
            prior.rank, prior.prior_rating, prior.leaderboard_name,
        )
    return list(rows.values())


def as_json(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, separators=(",", ":"))[:4000]
    except (TypeError, ValueError):
        return None


def now_date(now: datetime) -> date:
    return now.date()
