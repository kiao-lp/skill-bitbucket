"""Kiao credentials, Bitbucket Cloud requests, and bounded output."""

import base64
import http.client
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_ORIGIN = "https://api.bitbucket.org"
API_ROOT = "/2.0"
REDIRECTS = (301, 302, 303, 307, 308)
JSON_LIMIT = 4 * 1024 * 1024
PREVIEW_BYTES = 8192
CHUNK_BYTES = 65536
NETWORK_ERRORS = (URLError, TimeoutError, ConnectionError, http.client.HTTPException)


class InputError(Exception):
    pass


class BitbucketError(Exception):
    def __init__(self, message, *, status=None, empty_log=False):
        super().__init__(message)
        self.status = status
        self.empty_log = empty_log


@dataclass(frozen=True)
class Credentials:
    token: str = field(repr=False)
    email: str = field(repr=False, default="")
    workspace: str = ""
    repository: str = ""

    @classmethod
    def load(cls):
        root = Path(os.environ.get("KIAO_WORKER_ROOT_PATH") or "/tmp")
        state = Path(os.environ.get("KIAO_WORKER_STATE_PATH") or root / ".kiao-config")
        path = state / "atlassian.json"
        if not path.is_absolute():
            raise InputError("Kiao worker state path must be absolute")
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise InputError(f"Cannot read {path} ({exc.strerror})") from None
        except ValueError:
            raise InputError(f"Invalid JSON in {path}") from None

        config = config.get("bitbucket") if isinstance(config, dict) else None
        if not isinstance(config, dict):
            raise InputError(f"Set the bitbucket object in {path}")

        def setting(name):
            value = config.get(name)
            if value is None:
                return ""
            if not isinstance(value, str):
                raise InputError(f"bitbucket.{name} in {path} must be text")
            return value.strip()

        token, email = setting("token"), setting("email")
        if not token or any(not "!" <= ch <= "~" for ch in token):
            raise InputError(f"Set a valid bitbucket.token in {path}")
        if ":" in email or any(ch.isspace() or not ch.isprintable() for ch in email):
            raise InputError(f"Set a valid bitbucket.email in {path}")
        return cls(token, email, setting("workspace"), setting("repository"))

    def authorization(self):
        if self.email:
            encoded = base64.b64encode(f"{self.email}:{self.token}".encode()).decode("ascii")
            return "Basic " + encoded
        return "Bearer " + self.token


def https_url(url):
    if not isinstance(url, str) or any(ch.isspace() or not ch.isprintable() for ch in url):
        raise InputError("Expected an HTTPS URL")
    try:
        parts = urlsplit(url)
        if (parts.scheme == "https" and parts.hostname and parts.port in (None, 443)
                and parts.username is None and not parts.fragment and "\\" not in parts.netloc):
            return parts
    except ValueError:
        pass
    raise InputError("Expected an HTTPS URL")


def api_url(target):
    if isinstance(target, str) and target.startswith(API_ROOT + "/"):
        target = API_ORIGIN + target
    parts = https_url(target)
    if (parts.hostname != "api.bitbucket.org" or not parts.path.startswith(API_ROOT + "/")
            or any(segment in (".", "..") for segment in unquote(parts.path).split("/"))):
        raise InputError("Expected a Bitbucket Cloud /2.0/ API path or URL")
    return target


def with_query(url, query):
    if not query:
        return url
    parts = urlsplit(url)
    pairs = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key not in query]
    pairs.extend((key, value) for key, value in query.items() if value is not None)
    return urlunsplit(parts._replace(query=urlencode(pairs, doseq=True)))


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def projection(fields):
    paths = {}
    for field_name in fields.split(","):
        first, _, rest = field_name.partition(".")
        paths.setdefault(first, []).append(rest)
    return {key: projection(",".join(nested)) if all(nested) else {}
            for key, nested in paths.items()}


def select(value, fields):
    if not fields:
        return value
    if isinstance(value, list):
        return [select(item, fields) for item in value]
    if isinstance(value, dict):
        return {name: select(value[name], nested) for name, nested in fields.items() if name in value}
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Bitbucket:
    def __init__(self, credentials):
        self.credentials = credentials
        self.authorization = credentials.authorization()
        self.opener = build_opener(NoRedirect())

    def http_error(self, response):
        detail = ""
        try:
            payload = json.loads(response.read(4096))
            detail = payload["error"]["message"]
        except (ValueError, KeyError, TypeError, *NETWORK_ERRORS):
            pass
        if not isinstance(detail, str):
            detail = ""
        retry = response.headers.get("Retry-After")
        message = f"Bitbucket HTTP {response.code}"
        if detail:
            message += " " + self.redact(detail, 300)
        if retry:
            message += f" (Retry-After {self.redact(retry, 64)})"
        return BitbucketError(
            message,
            status=response.code,
            empty_log=(response.code == 416 and response.headers.get("Content-Range") == "bytes */0"),
        )

    def redact(self, text, limit):
        text = text.replace(self.authorization, "[redacted]")
        text = text.replace(self.authorization.split(" ", 1)[1], "[redacted]")
        text = text.replace(self.credentials.token, "[redacted]")
        return " ".join("".join(ch for ch in text if ch.isprintable() or ch.isspace()).split())[:limit]

    def open(self, target, *, method="GET", query=None, body=None, accept="application/json",
             byte_range=None, log_download=False):
        url = with_query(api_url(target), query)
        headers = {"Accept": accept}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json_text(body).encode("utf-8")
        if byte_range:
            headers["Range"] = byte_range

        anonymous = False
        for _ in range(4):
            request = Request(url, data=data, method=method, headers=headers)
            if not anonymous:
                request.add_unredirected_header("Authorization", self.authorization)
            try:
                return self.opener.open(request, timeout=30)
            except HTTPError as exc:
                with exc:
                    if method != "GET" or exc.code not in REDIRECTS:
                        raise self.http_error(exc) from None
                    location = exc.headers.get("Location")
                    try:
                        if not location:
                            raise InputError("Missing Location")
                        url = urljoin(url, location)
                        redirect = https_url(url)
                        same_api = redirect.hostname == "api.bitbucket.org" and redirect.path.startswith(API_ROOT + "/")
                        if same_api and not anonymous:
                            url = with_query(api_url(url), query)
                        elif log_download and (anonymous or exc.code == 307):
                            anonymous = True
                        else:
                            raise InputError("Unexpected redirect origin")
                    except (InputError, ValueError):
                        raise BitbucketError("Bitbucket returned an unsupported redirect") from None
        raise BitbucketError("Bitbucket redirected too many times")

    def object(self, target, fields, *, method="GET", body=None):
        with self.open(target, method=method, query={"fields": fields}, body=body) as response:
            if response.status == 204:
                return {"status": 204}
            if response.status == 202:
                location = response.headers.get("Location")
                result = {"status": 202}
                if location:
                    result["location"] = api_url(urljoin(API_ORIGIN, location))
                return result
            payload = read_json(response)
        if not isinstance(payload, dict):
            raise BitbucketError("Bitbucket returned an unexpected JSON response")
        return select(payload, projection(fields))

    def page(self, target, fields, *, next_page=None, page_size=10, query=None):
        page_fields = "next," + ",".join("values." + name for name in fields.split(","))
        if next_page:
            target = api_url(next_page)
            params = {"fields": page_fields}
        else:
            params = {"fields": page_fields, "pagelen": page_size, **(query or {})}
        with self.open(target, query=params) as response:
            payload = read_json(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("values"), list):
            raise BitbucketError("Bitbucket returned an invalid list response")
        result = {"items": select(payload["values"], projection(fields))}
        if payload.get("next"):
            result["next"] = api_url(payload["next"])
        return result


def read_json(response):
    data = response.read(JSON_LIMIT + 1)
    if len(data) > JSON_LIMIT:
        raise BitbucketError("Bitbucket JSON exceeded 4 MiB. Request fewer fields or a smaller page.")
    try:
        return json.loads(data)
    except ValueError:
        raise BitbucketError("Bitbucket returned invalid JSON") from None


def response_chunks(response):
    total = 0
    while chunk := response.read(CHUNK_BYTES):
        total += len(chunk)
        yield chunk
    expected = response.headers.get("Content-Length", "")
    if expected.isdecimal() and total != int(expected):
        raise BitbucketError("Bitbucket download ended early")


def tail(response, size):
    result = bytearray()
    for chunk in response_chunks(response):
        result.extend(chunk)
        del result[:-size]
    return result


def save_download(response, path):
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
            temporary_path = Path(file.name)
            for chunk in response_chunks(response):
                file.write(chunk)
            size = file.tell()
        os.link(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return {"path": str(path), "bytes": size}
