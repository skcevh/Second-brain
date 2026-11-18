"""Read-only Graph and Gmail collectors with durable per-account checkpoints."""
import base64
from datetime import UTC, datetime, timedelta
import email
from email import policy as email_policy
import json
from urllib.parse import quote, urlencode, urlsplit

from brain_core import encoded, iso, stamp
from brain_sources import header_mail_details, html_to_text, mail_record
from brain_store import stage
from mail_oauth import GMAIL_API, GRAPH_API, MailError, access_token, credential_path, request_json

GRAPH_FIELDS = ",".join((
    "id", "internetMessageId", "subject", "body", "from", "toRecipients", "ccRecipients",
    "isDraft", "sentDateTime", "receivedDateTime", "lastModifiedDateTime", "internetMessageHeaders",
))
SENSITIVITY = "Integer 0x0036"


def with_query(url, **params):
    return url + "?" + urlencode(params)


def mailbox(account, alias):
    return f"{alias} <{account['email']}>"


def cloud_store(alias, account):
    return f"cloud:{alias}:{account['provider']}:{account['email'].casefold()}"


def graph_address(item):
    value = (item or {}).get("emailAddress") or {}
    name, address = value.get("name"), value.get("address")
    return f"{name} <{address}>" if name and address and name != address else (address or name or "")


def graph_record(item, alias, account):
    if item.get("isDraft"):
        return None
    for prop in item.get("singleValueExtendedProperties") or []:
        if prop["id"] == SENSITIVITY and int(prop["value"]) >= 2:
            return None
    headers = {header["name"].casefold(): header["value"] for header in item.get("internetMessageHeaders") or []}
    if headers.get("sensitivity", "").strip().casefold() in {"private", "company-confidential"}:
        return None
    content_type = headers.get("content-type", "").casefold()
    if any(marker in content_type for marker in ("pkcs7-mime", "multipart/encrypted", "x-microsoft-rpmsg")):
        return None
    body = item.get("body") or {}
    text = body.get("content", "")
    if body.get("contentType", "").casefold() == "html":
        text = html_to_text(text)
    return mail_record({
        "id": f"graph:{alias}:{item['id']}", "internet_id": item.get("internetMessageId"),
        "subject": item.get("subject"), "body": text,
        "sender": graph_address(item.get("from")),
        "to": ", ".join(map(graph_address, item.get("toRecipients") or [])),
        "cc": ", ".join(map(graph_address, item.get("ccRecipients") or [])),
        "folder": mailbox(account, alias), "sent": item.get("sentDateTime") or item.get("receivedDateTime"),
        "modified": item.get("lastModifiedDateTime"),
    }, account["project"], store=cloud_store(alias, account))


def graph_pages(token, earliest, latest):
    lower, upper = (iso(value).replace("+00:00", "Z") for value in (earliest, latest))
    url = with_query(GRAPH_API + "/me/messages", **{
        "$select": GRAPH_FIELDS, "$top": 25, "$orderby": "lastModifiedDateTime asc",
        "$filter": f"lastModifiedDateTime ge {lower} and lastModifiedDateTime lt {upper}",
        "$expand": f"singleValueExtendedProperties($filter=id eq '{SENSITIVITY}')",
    })
    while url:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com" or parsed.path != "/v1.0/me/messages":
            raise ValueError("Onverwachte Graph-paginaverwijzing")
        page = request_json(url, token=token, headers={"Prefer": 'IdType="ImmutableId", outlook.body-content-type="text"'})
        yield from page["value"]
        url = page.get("@odata.nextLink")


def graph_collect(conn, token, account, alias, cursor):
    high = int(datetime.now(UTC).timestamp())
    initial = stamp(account["initial_since"])
    if high <= initial or (cursor and high <= cursor["high"]):
        raise ValueError("Controleer de begindatum en systeemklok van de mailbox.")
    low = max(initial, cursor["high"] - timedelta(minutes=15).total_seconds()) if cursor else initial
    count = stage(conn, (graph_record(item, alias, account) for item in graph_pages(token, low, high)))
    return count, {"high": high}


def gmail_ids(token, since=None, history=None):
    if history:
        collection = "history"
        params = {"startHistoryId": history, "historyTypes": "messageAdded", "maxResults": 500}
    else:
        collection = "messages"
        params = {"q": f"after:{int(since)}", "includeSpamTrash": "true", "maxResults": 500}
    ids = set()
    while True:
        page = request_json(with_query(f"{GMAIL_API}/{collection}", **params), token=token)
        if history:
            for event in page.get("history") or []:
                ids.update(entry["message"]["id"] for entry in event.get("messagesAdded") or [])
        else:
            ids.update(entry["id"] for entry in page.get("messages") or [])
        if not page.get("nextPageToken"):
            return ids, page.get("historyId")
        params["pageToken"] = page["nextPageToken"]


def gmail_record(token, mid, alias, account):
    url = with_query(f"{GMAIL_API}/messages/{quote(mid, safe='')}", format="raw")
    try:
        item = request_json(url, token=token)
    except MailError as exc:
        if exc.status != 404:
            raise
        return None  # Deleted between discovery and retrieval.
    if "DRAFT" in (item.get("labelIds") or []):
        return None
    raw = item["raw"]
    mime = email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)),
                                   policy=email_policy.default)
    details = header_mail_details(mime)
    if details is None:
        return None
    details.update(id=f"gmail:{alias}:{mid}", internet_id=str(mime.get("Message-ID") or ""),
                   folder=mailbox(account, alias))
    if not details["sent"]:
        details["sent"] = int(item.get("internalDate", 0))
    return mail_record(details, account["project"], store=cloud_store(alias, account))


def gmail_collect(conn, token, account, alias, cursor):
    history = cursor.get("history") if cursor else None
    if history:
        try:
            ids, latest = gmail_ids(token, history=history)
        except MailError as exc:
            if exc.status != 404:
                raise
            history = None
    if not history:
        # Capture this before listing so arrivals during the initial scan are picked up next time.
        latest = request_json(GMAIL_API + "/profile", token=token)["historyId"]
        ids, _ = gmail_ids(token, since=stamp(account["initial_since"]))
    if not latest:
        raise ValueError("Gmail gaf geen nieuwe synchronisatiepositie.")
    count = stage(conn, (gmail_record(token, mid, alias, account) for mid in sorted(ids)))
    return count, {"history": latest}


def collect_mail(conn, state, accounts):
    queued = errors = 0
    for alias, account in accounts.items():
        identity = encoded(account).decode()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not stamp(account["initial_since"]):
                raise ValueError("Stel initial_since in op een geldige datum.")
            token = access_token(state, alias, account)
            if token is None:
                conn.rollback()
                continue
            row = conn.execute("SELECT cursor FROM mail_sync WHERE alias=? AND identity=?",
                               (alias, identity)).fetchone()
            cursor = json.loads(row[0]) if row and row[0] else None
            collector = {"graph": graph_collect, "gmail": gmail_collect}[account["provider"]]
            count, saved = collector(conn, token, account, alias, cursor)
            conn.execute("""INSERT OR REPLACE INTO mail_sync VALUES (?, ?, ?, CURRENT_TIMESTAMP, NULL)""",
                         (alias, identity, encoded(saved).decode()))
            conn.commit()
            queued += count
        except (ValueError, OSError, TypeError, KeyError, AttributeError) as exc:
            conn.rollback()
            error = str(exc) if isinstance(exc, MailError) else type(exc).__name__
            conn.execute("""INSERT INTO mail_sync(alias,identity,last_error) VALUES (?,?,?)
                            ON CONFLICT(alias) DO UPDATE SET last_error=excluded.last_error""",
                         (alias, identity, error))
            conn.commit()
            errors += 1
    return queued, errors


def mail_status(state, accounts, conn=None):
    report = {}
    for alias, account in accounts.items():
        info = {"provider": account["provider"], "authorized": credential_path(state, alias).is_file()}
        if conn is not None:
            row = conn.execute("SELECT identity,last_success,last_error FROM mail_sync WHERE alias=?",
                               (alias,)).fetchone()
            if row:
                info["last_error"] = row["last_error"]
                if row["identity"] == encoded(account).decode():
                    info["last_success_utc"] = row["last_success"]
        report[alias] = info
    return report
