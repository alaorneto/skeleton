"""GitHub release sources (#498): the signed index and packages of a repository's releases.

The integrity never depends on GitHub: the index is signed and its entries pin the SHA-256 and the
signature of each package. This module only fetches bytes, under fixed rules: the API host comes
from the agent's file; the repository and tags are validated by regular expressions; an asset is
read only through its API URL, and the redirect to the download host is followed by hand, once,
over HTTPS, to an allowed host, without the token. The signed download URL and the token never
appear in an error, log or journal.
"""

import re
from collections.abc import Callable, Mapping
from email.message import Message
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .storage import AgentError, private_file

if TYPE_CHECKING:
    from .config import Config

REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}\Z")
INDEX_ASSET = "skeleton-index.json"
ASSET_NAME = re.compile(r"[A-Za-z0-9._-]{1,200}\Z")
TAG = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")


def valid_tag(value: Any) -> bool:
    return isinstance(value, str) and TAG.fullmatch(value) is not None and value not in (".", "..")


MAX_JSON_BYTES = 1024 * 1024
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_ASSETS = 200
DEFAULT_API = "https://api.github.com"
DEFAULT_DOWNLOAD_HOSTS = (".githubusercontent.com",)
REDIRECTS = (301, 302, 303, 307, 308)


class NoRedirect(HTTPRedirectHandler):
    """Refuse every redirect: catalogue downloads and GitHub API reads never follow one."""

    def redirect_request(
        self, request: Request, response: IO[bytes], code: int, message: str, headers: Message, newurl: str
    ) -> Request | None:
        raise AgentError("catalogue_redirect_rejected", 409)


class ManualRedirect(HTTPRedirectHandler):
    """Do not follow: the 3xx surfaces as an HTTPError whose Location the caller checks."""

    def redirect_request(
        self, request: Request, response: IO[bytes], code: int, message: str, headers: Message, newurl: str
    ) -> Request | None:
        return None


def valid_settings(value: Any) -> bool:
    """Shape of the optional ``github`` section of the agent configuration."""
    if not isinstance(value, dict) or set(value) - {"api_url", "download_hosts", "token_file"}:
        return False
    api = urlsplit(value.get("api_url", DEFAULT_API)) if isinstance(value.get("api_url", DEFAULT_API), str) else None
    if (
        api is None
        or api.scheme != "https"
        or not api.hostname
        or api.username
        or api.password
        or api.query
        or api.fragment
    ):
        return False
    hosts = value.get("download_hosts", list(DEFAULT_DOWNLOAD_HOSTS))
    if (
        not isinstance(hosts, list)
        or not hosts
        or not all(isinstance(host, str) and re.fullmatch(r"\.?[a-z0-9.-]+", host) for host in hosts)
    ):
        return False
    token = value.get("token_file")
    return token is None or (isinstance(token, str) and Path(token).is_absolute())


class GitHub:
    def __init__(self, config: "Config | Mapping[str, Any]", *, opener: Callable[..., Any] | None = None) -> None:
        settings = config.get("github") or {}
        self.api = str(settings.get("api_url", DEFAULT_API)).rstrip("/")
        self.download_hosts = tuple(settings.get("download_hosts") or DEFAULT_DOWNLOAD_HOSTS)
        self.token_file = settings.get("token_file")
        self.timeout = config.get("catalogue_timeout", 20)
        self._opener = opener

    def _open(self, handler: HTTPRedirectHandler, request: Request) -> Any:
        factory = self._opener or build_opener  # Read at call time: tests replace build_opener.
        return factory(handler).open(request, timeout=self.timeout)

    @staticmethod
    def _user_agent() -> str:
        from .selfupdate import agent_version

        return f"skeleton-deployment-agent/{agent_version() or 'unknown'}"

    def _token(self) -> str:
        """Read on every use, never kept; only ever sent to the API host."""
        try:
            token = private_file(self.token_file).read_text().strip() if self.token_file else ""
        except (OSError, AgentError):
            token = ""
        if not token or not token.isascii() or any(character.isspace() for character in token):
            raise AgentError("github_token_missing", 409)
        return token

    def _headers(self, accept: str, *, private: bool) -> dict[str, str]:
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": self._user_agent()}
        if private:
            headers["Authorization"] = f"Bearer {self._token()}"
        return headers

    @staticmethod
    def _repository(repository: str) -> str:
        if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
            raise AgentError("invalid_catalogue_source")
        return repository

    @staticmethod
    def _refusal(error: HTTPError, *, not_found: str) -> AgentError:
        headers = error.headers
        remaining = headers.get("x-ratelimit-remaining") if headers else None
        retry = headers.get("retry-after") if headers else None
        if error.code in (403, 429) and (str(remaining or "").strip() == "0" or retry):
            return AgentError("github_rate_limited", 409)
        if error.code == 404:
            return AgentError(not_found, 409)
        return AgentError("catalogue_unavailable", 409)

    @staticmethod
    def _read(response: Any, limit: int) -> bytes:
        content = response.read(limit + 1)
        if not isinstance(content, bytes):
            raise AgentError("catalogue_unavailable", 409)
        if len(content) > limit:
            raise AgentError("catalogue_download_too_large", 413)
        return content

    def _release(self, path: str, *, private: bool) -> dict[str, Any]:
        import json

        request = Request(self.api + path, headers=self._headers("application/vnd.github+json", private=private))  # noqa: S310 - HTTPS API host from the agent file
        try:
            with self._open(NoRedirect(), request) as response:
                content = self._read(response, MAX_JSON_BYTES)
        except HTTPError as error:
            raise self._refusal(error, not_found="github_release_not_found") from None
        except AgentError as error:
            if error.code in ("catalogue_download_too_large", "github_token_missing"):
                raise
            raise AgentError("catalogue_unavailable", 409) from None
        except Exception:  # noqa: BLE001 - any transport failure is catalogue_unavailable
            raise AgentError("catalogue_unavailable", 409) from None
        try:
            body = json.loads(content)
        except ValueError:
            raise AgentError("catalogue_unavailable", 409) from None
        if not isinstance(body, dict) or not valid_tag(body.get("tag_name")):
            raise AgentError("catalogue_unavailable", 409)
        assets = []
        listed = body.get("assets")
        for asset in (listed if isinstance(listed, list) else [])[:MAX_ASSETS]:
            if not isinstance(asset, dict):
                continue
            name, size, url = asset.get("name"), asset.get("size"), asset.get("url")
            if (
                isinstance(name, str)
                and ASSET_NAME.fullmatch(name)
                and type(size) is int
                and size >= 0
                and isinstance(url, str)
            ):
                assets.append({"name": name, "size": size, "url": url})  # browser_download_url is never used.
        return {"tag": body["tag_name"], "assets": assets}

    def latest_release(self, repository: str, *, private: bool = False) -> dict[str, Any]:
        return self._release(f"/repos/{self._repository(repository)}/releases/latest", private=private)

    def release_by_tag(self, repository: str, tag: str, *, private: bool = False) -> dict[str, Any]:
        if not valid_tag(tag):
            raise AgentError("catalogue_artifact_path_rejected")
        return self._release(f"/repos/{self._repository(repository)}/releases/tags/{tag}", private=private)

    def _allowed_location(self, location: str | None) -> str:
        parts = urlsplit(location or "")
        host = (parts.hostname or "").lower()
        try:
            port = parts.port
        except ValueError:
            port = -1
        allowed = any(host.endswith(entry) if entry.startswith(".") else host == entry for entry in self.download_hosts)
        if (
            parts.scheme != "https"
            or not host
            or parts.username
            or parts.password
            or port not in (None, 443)
            or not allowed
        ):
            raise AgentError("github_asset_url_rejected", 409)
        return str(location)

    def download_asset(self, repository: str, asset: Mapping[str, Any], *, limit: int, private: bool = False) -> bytes:
        prefix = f"{self.api}/repos/{self._repository(repository)}/releases/assets/"
        url = asset.get("url")
        if (
            not isinstance(url, str)
            or not url.lower().startswith(prefix.lower())
            or not url[len(prefix) :].isdigit()
            or not url[len(prefix) :].isascii()
        ):
            raise AgentError("github_asset_url_rejected", 409)
        if type(asset.get("size")) is not int or asset["size"] > limit:
            raise AgentError("catalogue_download_too_large", 413)
        request = Request(url, headers=self._headers("application/octet-stream", private=private))  # noqa: S310 - checked API prefix
        location = None
        try:
            with self._open(ManualRedirect(), request) as response:
                return self._read(response, limit)
        except HTTPError as error:
            if error.code not in REDIRECTS:
                raise self._refusal(error, not_found="github_asset_missing") from None
            location = self._allowed_location(error.headers.get("Location") if error.headers else None)
        except AgentError as error:
            if error.code in ("catalogue_download_too_large", "github_token_missing", "github_asset_url_rejected"):
                raise
            raise AgentError("catalogue_unavailable", 409) from None
        except Exception:  # noqa: BLE001 - any transport failure is catalogue_unavailable
            raise AgentError("catalogue_unavailable", 409) from None
        # A new request to the download host: never the Authorization header of the API request.
        signed = Request(location, headers={"User-Agent": self._user_agent(), "Accept": "application/octet-stream"})  # noqa: S310 - allowed HTTPS host
        try:
            with self._open(ManualRedirect(), signed) as response:
                return self._read(response, limit)
        except HTTPError as error:
            if error.code in REDIRECTS:
                raise AgentError("github_asset_url_rejected", 409) from None
            raise self._refusal(error, not_found="github_asset_missing") from None
        except AgentError as error:
            if error.code == "catalogue_download_too_large":
                raise
            raise AgentError("catalogue_unavailable", 409) from None
        except Exception:  # noqa: BLE001 - the signed URL never reaches the error
            raise AgentError("catalogue_unavailable", 409) from None


def find_asset(release: Mapping[str, Any], name: str, *, missing: str) -> dict[str, Any]:
    for asset in release.get("assets", []):
        if asset["name"] == name:
            return dict(asset)
    raise AgentError(missing, 409)


def artifact_reference(value: Any) -> tuple[str, str]:
    """``<tag>/<asset>`` of an index entry of a GitHub source; anything else is refused."""
    parts = value.split("/") if isinstance(value, str) else []
    if len(parts) != 2 or not valid_tag(parts[0]) or not ASSET_NAME.fullmatch(parts[1]) or parts[1] in (".", ".."):
        raise AgentError("catalogue_artifact_path_rejected")
    return parts[0], parts[1]
