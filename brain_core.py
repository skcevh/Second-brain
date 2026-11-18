"""Shared rules for private runtime data and normalized source records."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from datetime import UTC, datetime

SOURCES = {"chatgpt", "codex", "copilot-vscode", "copilot-cli", "outlook", "instructions"}
MAX_FILE = 256 * 1024**2
SECRET = re.compile(
    r"(?im)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
    r"|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}"
    r"|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{12,}|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b"
    r"|(?<=://)[^\s/@:]+:[^\s/@]+(?=@)"
    r"|\b(?:bearer)\s+[A-Za-z0-9_.~+/-]{12,}={0,2}"
    r"""|\b(?:api[-_ ]?key|password|passwd|client[-_ ]?secret|access[-_ ]?token|refresh[-_ ]?token|authorization)\s*['"]?\s*[:=]\s*(?:"[^"\r\n]+"|'[^'\r\n]+'|[^\s,;]+)"""
)
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def clean(value):
    if not isinstance(value, str):
        return ""
    value = CONTROL.sub("", value).encode("utf-8", "replace").decode("utf-8")
    return SECRET.sub("[GEHEIM GEWEERD]", value).strip()


def stamp(value):
    try:
        if isinstance(value, (int, float)) and math.isfinite(value):
            return value / 1000 if abs(value) >= 10**11 else float(value)
        if isinstance(value, str) and value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=parsed.tzinfo or UTC).timestamp()
    except (ValueError, OverflowError, OSError):
        pass
    return 0.0


def iso(value):
    try:
        return datetime.fromtimestamp(value, UTC).isoformat() if value else None
    except (ValueError, OSError, OverflowError):
        return None


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(read_bytes(path).decode("utf-8-sig"))


def read_bytes(path):
    if not regular(path):
        raise ValueError("Geen regulier bronbestand")
    with path.open("rb") as handle:
        data = handle.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ValueError("Bronbestand is groter dan de limiet")
    return data


def regular(path):
    try:
        info = path.lstat()
        return stat.S_ISREG(info.st_mode) and not (
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return False


def files(root, pattern, recursive=False):
    """Do not follow reparse points, including junctions."""
    def real_directory(path):
        try:
            info = path.lstat()
            return stat.S_ISDIR(info.st_mode) and not (
                getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except OSError:
            return False

    if not real_directory(root):
        return
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if real_directory(Path(directory) / name)] if recursive else []
        for name in names:
            path = Path(directory) / name
            if path.match(pattern) and regular(path):
                yield path


def outside_git(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Gebruik een absoluut gegevenspad")
    path = path.resolve()
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise ValueError("Gegevenspaden mogen niet in een Git-checkout staan")
    return path


def message(role, content, time=None, mid=None, refs=()):
    content = clean(content)
    if content:
        return {"role": role, "text": content, "at": stamp(time), "id": clean(mid),
                "refs": sorted({clean(ref) for ref in refs if isinstance(ref, str) and ref})}
    return None


def record(source, sid, title, project, messages, *, created=None, changed=None,
           cwd="", repository="", ref=""):
    sid, project = clean(sid), clean(project)
    messages = [item for item in messages if item]
    if source not in SOURCES or not sid or not project or not messages:
        return None
    created = stamp(created) or next((item["at"] for item in messages if item["at"]), 0)
    changed = max(stamp(changed), created, *(item["at"] for item in messages))
    return {"source": source, "id": sid, "title": clean(title) or messages[0]["text"].splitlines()[0][:160],
            "project": project, "created": created, "changed": changed, "cwd": clean(cwd),
            "repository": clean(repository), "ref": clean(ref), "messages": messages}


class Policy:
    def __init__(self, config):
        self.sources = config.get("sources", {})
        self.projects = config.get("projects", {})
        self.rules = sorted(
            ((Path(root).expanduser().resolve(), project)
             for project, roots in self.projects.items() for root in roots),
            key=lambda pair: len(pair[0].parts), reverse=True,
        )
        self.all_workspaces = config.get("allow_all_workspaces") is True
        self.denied = [Path(path).expanduser().resolve() for path in config.get("deny_paths", [])]
        self.approved_exports = config.get("approved_exports", {})
        self.reviewed = config.get("reviewed_drop_projects", [])
        self.instructions = config.get("instruction_files", {})
        self.outlook_stores = config.get("outlook_stores", {})

    def enabled(self, source):
        return self.sources.get(source) is True

    def workspace(self, value):
        if not isinstance(value, str) or not value:
            return None
        path = Path(value)
        if not path.is_absolute():
            return None
        path = path.resolve()
        if any(path.is_relative_to(root) for root in self.denied):
            return None
        for root, project in self.rules:
            if path.is_relative_to(root):
                return project
        return "Overige werkruimten" if self.all_workspaces else None

    def exported(self, source, sid, folder):
        selected = self.approved_exports.get(source, {}).get(sid)
        return selected or (folder if folder in self.reviewed else None)
