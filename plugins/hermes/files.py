"""Listing and downloading the files an agent produced. Nothing here runs unless asked.

Files are fetched one at a time from the signed owner API, which streams the bytes back
(no links are handed out). Each file is written straight to disk under a sanitised
name that never overwrites an existing file.
"""
from __future__ import annotations

import os
import re
import threading
import time
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


def session_dir() -> Path:
    """The Hermes session's working directory (not the gateway's), else the process's."""
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        return Path(resolve_agent_cwd())
    except Exception:
        return Path.cwd()


PROBE = ".crowdcode-write-check"


def _never_roots() -> list[Path]:
    """Folders a download never writes into, approved or not: Hermes's own home and autostart/service folders."""
    home = Path.home()
    roots = [home / ".config" / "autostart", home / ".config" / "systemd", home / "Library" / "LaunchAgents",
             home / "Library" / "LaunchDaemons", Path("/etc"), Path("/Library/LaunchAgents"),
             Path("/Library/LaunchDaemons")]
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup")
    try:
        from hermes_constants import get_hermes_home

        roots.append(Path(get_hermes_home()))
    except Exception:  # outside Hermes: the same default it uses
        roots.append(Path(os.environ.get("HERMES_HOME") or home / ".hermes"))
    return [r.resolve() for r in roots]


def write_denied(path: Path | str) -> str | None:
    """Why ``path`` must not be written, or None.

    First Hermes's own policy, ``agent.file_safety.get_write_denied_error`` (the check its file
    tools apply: credential and key files, ``.env`` files, Hermes's own runtime), then the
    folders in ``_never_roots``. A check that errors refuses the write.
    """
    try:
        resolved = Path(path).resolve()
        for root in _never_roots():
            if resolved == root or root in resolved.parents:
                return f"Write denied: '{path}' is inside {root}, where downloads are never saved."
    except Exception as exc:
        return f"Write denied: could not check '{path}' ({type(exc).__name__}: {exc})."
    try:
        from agent.file_safety import get_write_denied_error
    except ImportError:  # outside Hermes (plain tests); the rest of the checks still apply
        return None
    try:
        return get_write_denied_error(str(path))
    except Exception as exc:
        return f"Write denied: could not check '{path}' ({type(exc).__name__}: {exc})."


def dest_denied(directory: Path) -> str | None:
    """A folder is refused if writing it, or a file inside it, would be: some folders are only protected per file."""
    return write_denied(directory) or write_denied(directory / PROBE)


def plan_dest(dest: str, agent_name: str, base: Path) -> tuple[Path, bool]:
    """The folder files would be saved into, fully resolved, and whether it is inside ``base``."""
    root = Path(base).resolve()
    if dest:
        target = Path(os.path.expanduser(str(dest)))
        target = target if target.is_absolute() else root / target
    else:
        target = root / DEFAULT_DIR / safe_name(agent_name, "agent")
    target = target.resolve()  # follows symlinks in the parts that exist
    return target, (target == root or root in target.parents)


# Folders outside the session directory that the human approved through the pre_tool_call
# prompt, keyed by resolved path. The download handler consumes one; nothing else adds them.
_APPROVED: dict[str, float] = {}
_APPROVED_LOCK = threading.Lock()
APPROVAL_TTL = 600.0


def note_approval_requested(target: Path) -> None:
    with _APPROVED_LOCK:
        now = time.time()
        for key in [k for k, at in _APPROVED.items() if now - at > APPROVAL_TTL]:
            _APPROVED.pop(key, None)
        _APPROVED[str(target)] = now


def consume_approval(target: Path) -> bool:
    with _APPROVED_LOCK:
        at = _APPROVED.pop(str(target), None)
    return at is not None and time.time() - at <= APPROVAL_TTL


def prepare_dest(target: Path) -> Path:
    """Check the folder against Hermes's write policy, then create it and check it again."""
    denied = dest_denied(target)
    if denied:
        raise PermissionError(denied)
    target.mkdir(parents=True, exist_ok=True)
    final = target.resolve()
    denied = dest_denied(final) if final != target else None
    if denied:
        raise PermissionError(denied)
    return final


def unique_path(directory: Path, name: str) -> tuple[Path, Any]:
    """Create ``directory/name`` exclusively, adding ``-1``, ``-2``… when it exists. Returns (path, file)."""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    else:
        ext = "." + ext
    for n in range(0, 10000):
        candidate = directory / (name if n == 0 else f"{stem}-{n}{ext}")
        denied = write_denied(candidate)
        if denied:
            raise PermissionError(denied)
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

