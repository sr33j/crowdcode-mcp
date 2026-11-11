"""Listing and downloading the files an agent produced. Nothing here runs unless asked.

Files are fetched one at a time from the signed owner API, which streams the bytes back
(no links are handed out). Each file is written straight to disk under a sanitised
name that never overwrites an existing file.
"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import quote

DEFAULT_DIR = "crowdcode-files"
PAGE = 200
MAX_LIST = 2000
_BAD = re.compile(r'[<>:"|?*\x00-\x1f\x7f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_name(name: object, fallback: str = "file") -> str:
    """A single path component: no directories, no traversal, no control or reserved characters."""
    text = unicodedata.normalize("NFC", str(name or ""))
    text = text.replace("\\", "/").split("/")[-1]  # drop any directory part, either separator
    text = _BAD.sub("_", text).strip().strip(".")
    if not text or text in (".", ".."):
        text = fallback
    stem, dot, ext = text.rpartition(".")
    if (stem if dot else text).lower() in _RESERVED:
        text = "_" + text
    if len(text.encode()) > 200:  # keep the extension, trim the stem
        ext = ("." + ext) if dot and len(ext) <= 16 else ""
        text = text.encode()[: 200 - len(ext.encode())].decode(errors="ignore") + ext
    return text


def unique_path(directory: Path, name: str) -> tuple[Path, Any]:
    """Create ``directory/name`` exclusively, adding ``-1``, ``-2``… when it exists. Returns (path, file)."""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    else:
        ext = "." + ext
    for n in range(0, 10000):
        candidate = directory / (name if n == 0 else f"{stem}-{n}{ext}")
        try:
            return candidate, open(candidate, "xb")
        except FileExistsError:
            continue
    raise RuntimeError(f"too many files named {name} in {directory}")


def list_files(api: Any, agent_id: str, task_id: str = "") -> list[dict]:
    """Every ready file of the agent, newest first, following ``before`` pages."""
    files: list[dict] = []
    before = ""
    while len(files) < MAX_LIST:
        query = [f"limit={PAGE}"]
        if task_id:
            query.append(f"task_id={quote(task_id, safe='')}")
        if before:
            query.append(f"before={quote(before, safe='')}")
        page = api.signed("GET", f"/agents/{agent_id}/files?{'&'.join(query)}")
        if isinstance(page, dict):
            page = page.get("files", [])
        files.extend(page)
        if len(page) < PAGE or not page[-1].get("created_at"):
            break
        before = str(page[-1]["created_at"])
    return files


def resolve_dest(dest: str, agent_name: str, base: Path) -> Path:
    if dest:
        target = Path(os.path.expanduser(dest))
        target = target if target.is_absolute() else base / target
    else:
        target = base / DEFAULT_DIR / safe_name(agent_name, "agent")
    target.mkdir(parents=True, exist_ok=True)
    return target.resolve()


def download(api: Any, agent_id: str, file: dict, directory: Path, max_bytes: int) -> dict:
    """Stream one file into ``directory``; returns ``{id, name, path, size_bytes}``."""
    name = safe_name(file.get("name") or file.get("path"), f"file-{file.get('id', 'unknown')}")
    path, handle = unique_path(directory, name)
    written = 0
    try:
        with handle:
            for chunk in api.signed_stream(f"/agents/{agent_id}/files/{file['id']}"):
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError(f"{name} is larger than the {max_bytes // 1_000_000} MB download limit")
                handle.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {"id": file.get("id"), "name": path.name, "path": str(path), "size_bytes": written}

