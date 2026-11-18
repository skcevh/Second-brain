"""Read-only adapters for AI sessions and mail."""
import email
from email import policy as email_policy
from html.parser import HTMLParser
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit
import zipfile

from brain_core import MAX_FILE, digest, files, message, read_bytes, read_json, record, regular, stamp

UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", re.I)
CHATGPT_MEMBER = re.compile(r"conversations(?:[-_.](?:part[-_]?)?[0-9]+)?\.json$", re.I)
CODEX_WRAPPERS = ("<environment_context>", "<user_instructions>", "<permissions instructions>",
                   "<skill>", "<subagent_notification>", "<app-context>")


def objects(data):
    """Ignore a partially written final line in a live append log."""
    lines = data.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            value = json.loads(line.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            if index == len(lines) - 1 and not data.endswith(b"\n"):
                return
            raise ValueError("Ongeldig sessielog") from None
        if not isinstance(value, dict):
            raise ValueError("Onbekend sessielogformaat")
        yield value


def first_event(path):
    if not regular(path):
        return {}
    with path.open("rb") as handle:
        return next(objects(handle.readline(4 * 1024**2)), {})


def codex_events(path):
    """Stream large rollouts and skip known tool payloads before JSON decoding."""
    if not regular(path):
        return
    limit = 32 * 1024**2
    with path.open("rb") as handle:
        while line := handle.readline(limit):
            tool = re.search(rb'"payload"\s*:\s*\{\s*"type"\s*:\s*"[^"]*(?:call|output)[^"]*"', line[:500])
            if len(line) == limit and not line.endswith(b"\n"):
                while tail := handle.readline(limit):
                    if tail.endswith(b"\n"):
                        break
                if not tool:
                    raise ValueError("Te grote Codex-logregel buiten een toolrecord")
                continue
            if not tool:
                yield from objects(line)


def reference_urls(node):
    """Copy explicit citation or hyperlink URLs, excluding query and fragment secrets."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "url" and isinstance(value, str):
                try:
                    url = urlsplit(value)
                    if url.scheme in {"https", "http"} and url.hostname and not url.username:
                        yield url._replace(query="", fragment="").geturl()
                except ValueError:
                    continue
            elif isinstance(value, (list, dict)):
                yield from reference_urls(value)
    elif isinstance(node, list):
        for value in node:
            yield from reference_urls(value)


def chatgpt_rows(path):
    data = read_bytes(path)
    if path.suffix.lower() == ".json":
        groups = [json.loads(data.decode("utf-8-sig"))]
    else:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            members = [item for item in archive.infolist() if CHATGPT_MEMBER.fullmatch(Path(item.filename).name)]
            if not members or sum(item.file_size for item in members) > MAX_FILE:
                raise ValueError("Geen ondersteunde gesprekken in ZIP of te groot")
            groups = [json.loads(archive.read(item).decode("utf-8-sig")) for item in members]
    for group in groups:
        if not isinstance(group, list):
            raise ValueError("Onbekende ChatGPT-exportstructuur")
        yield from (item for item in group if isinstance(item, dict))


def chatgpt(path, rules, folder):
    for chat in chatgpt_rows(path):
        sid = chat.get("conversation_id") or chat.get("id")
        if not isinstance(sid, str):
            continue
        project = rules.exported("chatgpt", sid, folder)
        if not project:
            continue
        mapping = chat.get("mapping") or {}
        # The export mapping contains edits and branches. Preserve each visible message once.
        entries = [node.get("message") for node in mapping.values() if isinstance(node, dict)]
        entries = [entry for entry in entries if isinstance(entry, dict)]
        entries.sort(key=lambda entry: stamp(entry.get("create_time")))
        messages = []
        for entry in entries:
            role = (entry.get("author") or {}).get("role")
            metadata = entry.get("metadata") or {}
            if role not in {"user", "assistant"} or metadata.get("is_visually_hidden_from_conversation"):
                continue
            parts = (entry.get("content") or {}).get("parts") or []
            body = "\n".join(part for part in parts if isinstance(part, str))
            refs = reference_urls(metadata.get("citations", []) + metadata.get("content_references", [])
                                  if isinstance(metadata.get("citations", []), list)
                                  and isinstance(metadata.get("content_references", []), list) else [])
            messages.append(message(role, body, entry.get("create_time"), entry.get("id"), refs))
        ref = f"https://chatgpt.com/c/{sid}" if UUID.fullmatch(sid) else f"chatgpt:{sid}"
        yield record("chatgpt", sid, chat.get("title"), project, messages,
                     created=chat.get("create_time"), changed=chat.get("update_time"), ref=ref)


def codex(path, rules):
    first = first_event(path)
    meta = first.get("payload", {}) if first.get("type") == "session_meta" else {}
    cwd = meta.get("cwd")
    project = rules.workspace(cwd)
    sid = meta.get("id") or meta.get("session_id")
    if not project or not isinstance(sid, str):
        return
    messages = []
    fallback = []
    for event in codex_events(path):
        payload = event.get("payload") or {}
        event_type = event.get("type")
        if event_type == "turn_context" and payload.get("cwd") and rules.workspace(payload["cwd"]) != project:
            return
        role = payload.get("role")
        if event_type == "response_item" and payload.get("type") == "message" and role in {"user", "assistant"}:
            body = "\n".join(part["text"] for part in payload.get("content", [])
                             if isinstance(part, dict) and part.get("type") in {"input_text", "output_text", "text"}
                             and isinstance(part.get("text"), str))
            if not body.lstrip().startswith(CODEX_WRAPPERS):
                # Embedded environment context is generated by the harness.
                body = body.split("<environment_context>", 1)[0]
                messages.append(message(role, body, event.get("timestamp"), payload.get("id")))
        elif event_type == "event_msg" and payload.get("type") in {"user_message", "agent_message"}:
            role = "user" if payload["type"] == "user_message" else "assistant"
            body = payload.get("message")
            if isinstance(body, str) and not body.lstrip().startswith(CODEX_WRAPPERS):
                fallback.append(message(role, body.split("<environment_context>", 1)[0],
                                        event.get("timestamp"), payload.get("id")))
    git = meta.get("git") or {}
    yield record("codex", sid, meta.get("title"), project, [msg for msg in messages if msg] or fallback,
                 created=meta.get("timestamp"), cwd=cwd, repository=git.get("repository_url"),
                 ref=f"codex:{sid}")


def copilot_cli(path, rules):
    first = first_event(path)
    data = first.get("data", {}) if first.get("type") == "session.start" else {}
    context = data.get("context") or {}
    cwd = context.get("cwd") or data.get("cwd")
    project = rules.workspace(cwd)
    sid = data.get("sessionId") or path.parent.name
    if not data or not project or not isinstance(sid, str):
        return
    messages = []
    for event in objects(read_bytes(path)):
        content = event.get("data") or {}
        kind = event.get("type")
        if kind in {"user.message", "assistant.message"} and not event.get("agentId") and not content.get("parentToolCallId"):
            role = "user" if kind == "user.message" else "assistant"
            messages.append(message(role, content.get("content"), event.get("timestamp"), event.get("id")))
    yield record("copilot-cli", sid, data.get("name"), project, messages,
                 created=first.get("timestamp"), cwd=cwd,
                 repository=context.get("repository") or context.get("repositoryUrl"),
                 ref=f"copilot:{sid}")


def vscode_data(path):
    if path.suffix.lower() == ".json":
        return read_json(path)
    state = None
    for operation in objects(read_bytes(path)):
        kind = operation.get("kind")
        if kind == 0 and state is None:
            state = operation["v"]
            continue
        keys = operation.get("k")
        if state is None or not isinstance(keys, list) or not keys:
            raise ValueError("Onbekend VS Code-operatielog")
        parent = state
        for key in keys[:-1]:
            parent = parent[key]
        key = keys[-1]
        if kind == 1:
            parent[key] = operation["v"]
        elif kind == 2 and isinstance(parent, dict):
            parent.pop(key, None)
        elif kind == 3:
            target = parent[key]
            if "i" in operation:
                del target[operation["i"]:]
            target.extend(operation.get("v", []))
        else:
            raise ValueError("Onbekende VS Code-operatie")
    return state or {}


def local_uri(value):
    if isinstance(value, dict) and value.get("scheme") == "file" and not value.get("authority"):
        value = value.get("path")
    if not isinstance(value, str):
        return ""
    if value.startswith("file:"):
        uri = urlsplit(value)
        if uri.netloc:
            return ""
        value = unquote(uri.path)
    return value[1:] if re.match(r"^/[a-z]:", value, re.I) else value


def vscode_id(chat, path=None):
    sid = chat.get("sessionId")
    uri = chat.get("sessionResource") or {}
    uri = uri.get("path", "") if isinstance(uri, dict) else uri
    if isinstance(uri, str) and "copilotcli" in uri.lower():
        match = UUID.search(uri)
        if match:
            return match[0].lower()
    requests = chat.get("requests") or []
    # Modern exported sessions retain a session ID. For old exports use the first request ID.
    if not isinstance(sid, str):
        sid = next((entry.get("requestId") for entry in requests if isinstance(entry.get("requestId"), str)), None)
    if not sid:
        if path:
            sid = path.stem
        elif requests:
            first = requests[0]
            sid = "legacy-" + digest(json.dumps(
                [chat.get("creationDate"), first.get("timestamp"), first.get("message")],
                sort_keys=True, ensure_ascii=True).encode())
    return sid.lower() if sid and UUID.fullmatch(sid) else sid


def vscode(path, rules, folder=None, *, local=True):
    workspace = ""
    if local:
        descriptor = path.parent.parent / "workspace.json"
        if not regular(descriptor):
            return
        data = read_json(descriptor)
        workspace = local_uri(data.get("folder"))
        # A multi-root workspace may include customer roots outside the allowlist.
        if not rules.workspace(workspace):
            return
    chat = vscode_data(path)
    requests = chat.get("requests")
    if not isinstance(requests, list):
        raise ValueError("Onbekend VS Code-chatbestand")
    sid = vscode_id(chat, path if local else None)
    if not sid:
        return
    project = rules.workspace(workspace) if local else rules.exported("copilot-vscode", sid, folder)
    if not project:
        return
    cwd = local_uri(chat.get("workingDirectory")) or workspace
    if local and rules.workspace(cwd) != project:
        return
    messages = []
    for request in requests:
        prompt = request.get("message")
        prompt = prompt.get("text") if isinstance(prompt, dict) else prompt
        at = request.get("timestamp")
        messages.append(message("user", prompt, at, request.get("requestId")))
        response = request.get("response") or []
        if isinstance(response, str):
            response = [response]
        parts = []
        for part in response:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("kind") in {None, "markdownContent"}:
                content = part.get("content", part)
                value = content.get("value") if isinstance(content, dict) else content
                if isinstance(value, str):
                    parts.append(value)
        messages.append(message("assistant", "\n".join(parts), at, request.get("responseId"),
                                reference_urls(request.get("contentReferences") or [])))
    row = record("copilot-vscode", sid, chat.get("customTitle") or chat.get("title"), project, messages,
                 created=chat.get("creationDate"), changed=chat.get("lastMessageDate"), cwd=cwd,
                 ref=f"copilot:{sid}")
    if row and not local:
        row["is_export"] = True
    yield row


def vscode_export(path, rules, folder):
    yield from vscode(path, rules, folder, local=False)


class MailHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        if tag in {"br", "p", "tr", "div"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def mail_record(mail, project, *, store=""):
    sid = mail.get("internet_id") or mail.get("id")
    if not isinstance(sid, str) or not sid.strip():
        return None
    sid = sid.strip().strip("<>")
    context = "\n".join(f"{label}: {mail[key]}" for key, label in
                        (("sender", "Van"), ("to", "Aan"), ("cc", "Cc"), ("folder", "Map"))
                        if isinstance(mail.get(key), str) and mail[key])
    body = mail.get("body") or ""
    return record("outlook", sid, mail.get("subject"), project,
                  [message("mail", f"{context}\n\n{body}", mail.get("sent"), sid)],
                  created=mail.get("sent"), changed=mail.get("modified"), cwd=store, ref=f"message-id:{sid}")


def html_to_text(text):
    parser = MailHTML()
    parser.feed(text)
    return "".join(parser.parts)


def header_mail_details(mail):
    if str(mail.get("Sensitivity", "")).strip().casefold() in {"private", "company-confidential"}:
        return None
    if any(marker in mail.get_content_type() for marker in ("pkcs7-mime", "multipart/encrypted", "x-microsoft-rpmsg")):
        return None
    part = mail.get_body(preferencelist=("plain", "html"))
    body = ""
    if part:
        try:
            body = part.get_content()
        except LookupError:
            body = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    if part and part.get_content_type() == "text/html":
        body = html_to_text(body)
    try:
        sent = email.utils.parsedate_to_datetime(str(mail.get("Date"))).isoformat()
    except (ValueError, TypeError, OverflowError):
        sent = None
    return {"subject": str(mail.get("Subject", "")), "body": body, "sender": str(mail.get("From", "")),
            "to": str(mail.get("To", "")), "cc": str(mail.get("Cc", "")), "sent": sent}


def eml(path, rules, folder):
    raw = read_bytes(path)
    mail = email.message_from_bytes(raw, policy=email_policy.default)
    sid = str(mail.get("Message-ID") or digest(raw))
    project = rules.exported("outlook", sid, folder)
    if not project:
        return
    details = header_mail_details(mail)
    if details is not None:
        yield mail_record({"internet_id": sid, **details}, project)


def mapi_value(item, identifier, kind):
    for entry_set in item.record_sets:
        for entry in entry_set.entries:
            if entry.entry_type == identifier:
                return getattr(entry, "data_as_" + kind)
    return None


def pst_body(item, field):
    try:
        return getattr(item, field)
    except OSError as exc:
        # libpff reports an empty MAPI body value as an error in some Outlook exports.
        if "missing value data." not in str(exc):
            raise
        return None


def pst_folder(folder, project, folder_path=""):
    folder_path = "/".join(filter(None, (folder_path, folder.name)))
    for item in folder.sub_messages:
        mail_class = mapi_value(item, 0x001A, "string") or ""
        if not mail_class.casefold().startswith("ipm.note") or any(
            marker in mail_class.casefold() for marker in ("smime", "rpmsg")
        ) or (mapi_value(item, 0x0036, "integer") or 0) >= 2:
            continue
        transport = item.transport_headers
        headers = email.message_from_string(transport or "", policy=email_policy.default)
        if str(headers.get("Sensitivity", "")).casefold().strip() in {"private", "company-confidential"}:
            continue
        body = pst_body(item, "plain_text_body")
        html = pst_body(item, "html_body") if not body else None
        if html:
            parser = MailHTML()
            parser.feed(html.decode("utf-8", "replace") if isinstance(html, bytes) else html)
            body = "".join(parser.parts)
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        sent = item.client_submit_time or item.delivery_time or item.creation_time
        modified = item.modification_time
        sid = str(headers.get("Message-ID") or mapi_value(item, 0x1035, "string") or "")
        fallback = digest(repr((item.subject, item.sender_name, sent, body)).encode())
        yield mail_record({
            "internet_id": sid, "id": fallback, "subject": item.subject, "body": body,
            "sender": str(headers.get("From") or item.sender_name or ""),
            "to": str(headers.get("To") or mapi_value(item, 0x0E04, "string") or ""),
            "cc": str(headers.get("Cc") or mapi_value(item, 0x0E03, "string") or ""),
            "folder": folder_path, "sent": sent.isoformat() if sent else None,
            "modified": modified.isoformat() if modified else None,
        }, project)
    for child in folder.sub_folders:
        yield from pst_folder(child, project, folder_path)


def pst(path, rules, folder):
    if folder not in rules.reviewed or not regular(path):
        return
    vendor = Path(__file__).with_name("vendor")
    if vendor.is_dir() and str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))
    try:
        import pypff
    except ImportError:
        raise ValueError("PST-import vereist de optionele libpff-installatie.") from None
    archive = pypff.file()
    archive.open(str(path), "r")
    try:
        yield from pst_folder(archive.root_folder, folder)
    finally:
        archive.close()


def outlook_export(path, rules, folder):
    adapter = pst if path.suffix.lower() == ".pst" else eml
    yield from adapter(path, rules, folder)


def discover(root_overrides=None):
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", home))
    codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex"))
    copilot_home = Path(os.environ.get("COPILOT_HOME", home / ".copilot"))
    defaults = {
        "codex": [codex_home / "sessions", codex_home / "archived_sessions"],
        "copilot-cli": [copilot_home / "session-state"],
        "copilot-vscode": [appdata / edition / "User" / "workspaceStorage"
                           for edition in ("Code", "Code - Insiders", "VSCodium")],
    }
    return {source: [Path(root) for root in (root_overrides or {}).get(source, roots)]
            for source, roots in defaults.items()}


def local_files(source, roots):
    pattern = {"codex": "rollout-*.jsonl", "copilot-cli": "events.jsonl",
               "copilot-vscode": "*.json*"}[source]
    for root in roots:
        if not root.is_symlink():
            for path in files(root, pattern, recursive=True):
                relative = path.relative_to(root)
                if source == "copilot-cli" and len(relative.parts) != 2:
                    continue
                if source == "copilot-vscode" and (len(relative.parts) != 3
                                                  or path.parent.name != "chatSessions"
                                                  or path.suffix not in {".json", ".jsonl"}):
                    continue
                yield path
