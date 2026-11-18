"""Delegated sign-in and Windows user-scoped storage for mailbox credentials."""
import base64
import ctypes
from ctypes import wintypes
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
import secrets
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID
import webbrowser

from brain_core import encoded, outside_git, read_bytes, read_json
from brain_store import publish

GRAPH_API = "https://graph.microsoft.com/v1.0"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
MICROSOFT = "https://login.microsoftonline.com/common/oauth2/v2.0"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GRAPH_SCOPE = "https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/User.Read offline_access"
ENDPOINTS = {
    "graph": (MICROSOFT + "/authorize", MICROSOFT + "/token", GRAPH_SCOPE),
    "gmail": ("https://accounts.google.com/o/oauth2/v2/auth", "https://oauth2.googleapis.com/token", GOOGLE_SCOPE),
}
ALIAS = re.compile(r"[a-z][a-z0-9-]{0,40}\Z")


class MailError(ValueError):
    def __init__(self, status, code=""):
        self.status = status
        self.code = code if re.fullmatch(r"[\w.-]{1,80}", code) else ""
        super().__init__(f"mailprovider HTTP {status}" + (f" ({self.code})" if self.code else ""))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MailError(code, "unexpected_redirect")


def request_json(url, *, token=None, form=None, headers=None):
    extra = {"Accept": "application/json", **(headers or {})}
    if token:
        extra["Authorization"] = "Bearer " + token
    if form is not None:
        extra["Content-Type"] = "application/x-www-form-urlencoded"
    request = Request(url, data=urlencode(form).encode() if form is not None else None, headers=extra)
    try:
        with build_opener(NoRedirect()).open(request, timeout=35) as response:
            limit = 64 * 1024**2
            data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError("Mailproviderrespons is te groot")
            return json.loads(data)
    except HTTPError as exc:
        code = ""
        with exc:
            try:
                body = json.loads(exc.read(16384))
                error = body.get("error")
                code = error.get("code", "") if isinstance(error, dict) else (error or "")
            except (AttributeError, ValueError):
                pass
        raise MailError(exc.code, code if isinstance(code, str) else "") from None


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(payload, seal):
    if os.name != "nt":
        raise ValueError("Mailcredentials vereisen Windows DPAPI")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    source = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
    original, destination = Blob(len(payload), source), Blob()
    function = crypt.CryptProtectData if seal else crypt.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(original), None, None, None, None, 1, ctypes.byref(destination)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(destination.data, destination.size)
    finally:
        kernel.LocalFree(destination.data)


def credential_path(state, alias):
    if not ALIAS.fullmatch(alias):
        raise ValueError("Ongeldige mailbox-alias")
    return state / f"mail-{alias}.dpapi"


def save_credentials(state, alias, credentials):
    path = credential_path(state, alias)
    publish(path.parent, path.name, dpapi(encoded(credentials), True))


def load_credentials(state, alias):
    path = credential_path(state, alias)
    if not path.exists():
        return None
    return json.loads(dpapi(read_bytes(path), False))


def session_state(credentials, response):
    return {
        **credentials,
        "access_token": response["access_token"],
        "refresh_token": response.get("refresh_token") or credentials.get("refresh_token"),
        "expires_at": time.time() + int(response.get("expires_in", 3600)),
    }


def provider_endpoints(provider, authority=None):
    authorize, token, scope = ENDPOINTS[provider]
    if provider == "graph" and authority == "consumers":
        authorize = authorize.replace("/common/", "/consumers/")
        token = token.replace("/common/", "/consumers/")
    elif authority not in (None, "common"):
        raise ValueError("Ongeldig Microsoft-aanmeldpunt")
    return authorize, token, scope


def token_request(provider, credentials, *, authority=None, **fields):
    form = {"client_id": credentials["client_id"], **fields}
    if provider == "graph":
        form["scope"] = GRAPH_SCOPE
    elif credentials.get("client_secret"):
        form["client_secret"] = credentials["client_secret"]
    return request_json(provider_endpoints(provider, authority)[1], form=form)


def access_token(state, alias, account):
    credentials = load_credentials(state, alias)
    if credentials is None:
        return None
    provider = account["provider"]
    if credentials["provider"] != provider or credentials["email"] != account["email"].casefold():
        raise ValueError("Mailboxconfiguratie is gewijzigd; verbind de mailbox opnieuw.")
    if credentials["expires_at"] <= time.time() + 60:
        response = token_request(provider, credentials, authority=account.get("authority"), grant_type="refresh_token",
                                 refresh_token=credentials["refresh_token"])
        credentials = session_state(credentials, response)
        save_credentials(state, alias, credentials)
    return credentials["access_token"]


def client_registration(provider, client_id, credentials_file):
    if provider == "graph" and client_id and not credentials_file:
        return {"client_id": str(UUID(client_id))}
    if provider == "gmail" and credentials_file and not client_id:
        installed = read_json(outside_git(credentials_file)).get("installed")
        if not isinstance(installed, dict):
            raise ValueError("Gebruik het OAuth-clientbestand van het type Desktop app")
        cid = installed.get("client_id", "")
        if not cid.endswith(".apps.googleusercontent.com"):
            raise ValueError("Ongeldige Gmail OAuth-client-ID")
        return {"client_id": cid, "client_secret": installed.get("client_secret")}
    raise ValueError("Gebruik --client-id voor Graph of --credentials-file voor Gmail")


def interactive_code(provider, account, client_id):
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    expected_state = secrets.token_urlsafe(32)
    result = {}
    authorize, _, scope = provider_endpoints(provider, account.get("authority"))

    class Callback(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def log_message(self, *_args):
            pass

        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            state = query.get("state", [""])[0]
            valid = urlsplit(self.path).path == "/" and secrets.compare_digest(
                state.encode("utf-8"), expected_state.encode("ascii"))
            if valid:
                result.update(error=query.get("error", [None])[0], code=query.get("code", [None])[0])
            body = ("Aanmelding ontvangen. Volg het resultaat in PowerShell." if valid else
                    "Deze aanvraag hoort niet bij de aanmelding.").encode()
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        host = "localhost" if provider == "graph" else "127.0.0.1"
        redirect = f"http://{host}:{server.server_port}/"
        params = {"client_id": client_id, "redirect_uri": redirect, "response_type": "code",
                  "scope": scope, "state": expected_state, "code_challenge": challenge,
                  "code_challenge_method": "S256", "login_hint": account["email"]}
        if provider == "gmail":
            params.update(access_type="offline", prompt="consent select_account")
        else:
            params.update(prompt="select_account")
        url = authorize + "?" + urlencode(params)
        print("Meld je in de browser aan en verleen leestoegang.", flush=True)
        if not webbrowser.open(url):
            print(url, flush=True)
        deadline = time.monotonic() + 300
        server.timeout = 1
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result.get("code") or result.get("error"):
        raise ValueError("Aanmelding geannuleerd, geweigerd of verlopen.")
    return result["code"], verifier, redirect


def connect(state, alias, account, client_id, credentials_file):
    credential_path(state, alias)
    provider = account["provider"]
    registration = client_registration(provider, client_id, credentials_file)
    code, verifier, redirect = interactive_code(provider, account, registration["client_id"])
    response = token_request(provider, registration, authority=account.get("authority"),
                             code=code, code_verifier=verifier, redirect_uri=redirect,
                             grant_type="authorization_code")
    expected = account["email"].casefold()
    token = response["access_token"]
    if provider == "graph":
        identity = request_json(GRAPH_API + "/me?$select=mail,userPrincipalName", token=token)
        identities = [identity.get("mail"), identity.get("userPrincipalName")]
    else:
        identity = request_json(GMAIL_API + "/profile", token=token)
        identities = [identity.get("emailAddress")]
    if expected not in [(value or "").casefold() for value in identities]:
        raise ValueError("Het gekozen browseraccount komt niet overeen met de ingestelde mailbox.")
    if not response.get("refresh_token"):
        raise ValueError("De provider gaf geen toestemming voor toegang op de achtergrond.")
    credentials = {"provider": provider, "email": expected, **registration}
    save_credentials(state, alias, session_state(credentials, response))
    return {"mailbox": alias, "connected": True}
