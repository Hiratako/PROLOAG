"""Optional, fail-closed Google Sheets transport for app-owned publications.

Importing this module performs no I/O and requires no Google packages. OAuth
and API calls happen only through explicit methods. Tokens/client configuration
are DPAPI-encrypted outside the project; no HTTP/OAuth exception body is exposed.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import ctypes
from ctypes import wintypes
import hashlib
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
import webbrowser
from wsgiref.simple_server import WSGIRequestHandler, make_server


SCOPES = ("https://www.googleapis.com/auth/drive.file",)
API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
DOCS_API_BASE = "https://docs.googleapis.com/v1/documents"
AUTH_URIS = {"https://accounts.google.com/o/oauth2/auth", "https://accounts.google.com/o/oauth2/v2/auth"}
TOKEN_URI = "https://oauth2.googleapis.com/token"
ROLES = ("overview", "daily_summary", "subjects", "materials", "categories", "study_sessions", "manifest")
SHEET_IDS = {role: 7100 + index for index, role in enumerate(ROLES)}
SHEET_TITLES = {role: f"PROLOAG_{role}" for role in ROLES}
MANIFEST_COLUMNS = ("snapshot_id", "payload_hash", "schema_version", "generated_at", "period", "options", "counts")
HTTP_TIMEOUT = 30
OAUTH_TIMEOUT = 120
MAX_REQUEST_BYTES = 1_800_000
_BOOK_ID = re.compile(r"^[A-Za-z0-9_-]{5,160}$")
_HEX_ID = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_LOCK = threading.RLock()


class GoogleShareError(ValueError):
    def __init__(self, message: str, *, code: str = "google_share", uncertain: bool = False):
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise GoogleShareError("送信データの形式が不正です。", code="invalid_snapshot") from None


def publication_hash(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(snapshot)).hexdigest()


@contextmanager
def _no_sensitive_logs():
    # OAuth libraries can log callback URLs/token bodies at debug level. Keep
    # their exceptions and debug output out of the application's logs entirely.
    with _SENSITIVE_LOCK:
        previous = logging.root.manager.disable
        logging.disable(max(previous, logging.CRITICAL))
        try:
            yield
        finally:
            logging.disable(previous)


def _optional_dependencies_directory() -> Path:
    return Path(__file__).resolve().parents[1] / f".deps-google{sys.version_info.major}{sys.version_info.minor}"


def _enable_optional_dependency_path() -> None:
    # Keep Google's namespace packages separate from the main target directory
    # (which may already contain google.protobuf). No import-time path changes.
    directory = _optional_dependencies_directory()
    if directory.is_dir() and str(directory) not in sys.path:
        sys.path.append(str(directory))
        importlib.invalidate_caches()


def dependencies_available() -> bool:
    try:
        _enable_optional_dependency_path()
        return all(importlib.util.find_spec(name) is not None for name in ("google.auth", "google_auth_oauthlib", "requests"))
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _project_key(root: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(Path(root).resolve())).encode("utf-8")).hexdigest()


def _private_directory(root: Path) -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if os.name != "nt" or not base:
        raise GoogleShareError("この接続の安全な保存にはWindows DPAPIが必要です。", code="unsupported_platform")
    directory = (Path(base) / "PROLOAG" / "google" / _project_key(root)).resolve()
    if directory.is_relative_to(Path(root).resolve()):
        raise GoogleShareError("認証情報をプロジェクト内へ保存できません。", code="unsafe_private_path")
    return directory


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise GoogleShareError("Windows DPAPIを利用できません。", code="unsupported_platform")
    buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = _DataBlob()
    entropy_buffer = ctypes.create_string_buffer(b"PROLOAG Google connection v1")
    entropy = _DataBlob(len(entropy_buffer.value), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    blob_pointer = ctypes.POINTER(_DataBlob)
    crypt.CryptProtectData.argtypes = [blob_pointer, wintypes.LPCWSTR, blob_pointer, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_pointer]
    crypt.CryptUnprotectData.argtypes = [blob_pointer, ctypes.c_void_p, blob_pointer, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_pointer]
    crypt.CryptProtectData.restype = wintypes.BOOL
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(result))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), "PROLOAG", ctypes.byref(entropy), None, None, 1, ctypes.byref(result))
    if not ok:
        raise GoogleShareError("認証情報の暗号化・復号に失敗しました。", code="private_storage")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


class _PrivateVault:
    def __init__(self, root: Path):
        self.directory = _private_directory(root)
        self.path = self.directory / "connection.dpapi"

    def read(self) -> dict[str, Any] | None:
        try:
            if not self.path.exists():
                return None
            if self.path.is_symlink() or self.path.stat().st_size > 1_000_000:
                raise ValueError("invalid vault")
            payload = json.loads(_dpapi(self.path.read_bytes(), decrypt=True).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("invalid vault")
            return payload
        except Exception:
            raise GoogleShareError("保存済みGoogle接続を読み取れません。再接続してください。", code="private_storage") from None

    def write(self, payload: dict[str, Any]) -> None:
        temporary = None
        try:
            encrypted = _dpapi(canonical_json(payload))
            self.directory.mkdir(parents=True, exist_ok=True)
            if self.directory.is_symlink() or self.path.is_symlink():
                raise ValueError("unsafe path")
            with tempfile.NamedTemporaryFile(dir=self.directory, prefix="connection-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except Exception:
            raise GoogleShareError("Google接続を安全に保存できませんでした。", code="private_storage") from None
        finally:
            try:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            except OSError:
                pass  # A leftover temporary file contains ciphertext only.

    def delete(self) -> None:
        try:
            if self.path.is_symlink():
                raise ValueError("unsafe path")
            self.path.unlink(missing_ok=True)
        except Exception:
            raise GoogleShareError("保存済み接続を解除できませんでした。", code="private_storage") from None


def _validate_client_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict) or not isinstance(config.get("installed"), dict):
        raise GoogleShareError("Desktopアプリ用のOAuth設定JSONを指定してください。", code="invalid_oauth_config")
    source = config["installed"]
    if source.get("auth_uri") not in AUTH_URIS or source.get("token_uri") != TOKEN_URI:
        raise GoogleShareError("Google以外の認証URLは利用できません。", code="invalid_oauth_config")
    if not all(isinstance(source.get(key), str) and source[key] and len(source[key]) <= 4096 for key in ("client_id", "client_secret")):
        raise GoogleShareError("OAuth設定に必要な項目がありません。", code="invalid_oauth_config")
    return {"installed": {key: source[key] for key in ("client_id", "client_secret", "auth_uri", "token_uri")}}


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)


def _run_oauth(flow: Any, *, timeout_seconds: float = OAUTH_TIMEOUT) -> Any:
    callback: dict[str, str] = {}
    expected_state = ""
    origin = ""

    def application(environ: dict[str, Any], start_response: Any):
        query = str(environ.get("QUERY_STRING", ""))
        try:
            params = parse_qs(query, max_num_fields=20) if len(query) <= 16000 else {}
        except ValueError:
            params = {}
        state = params.get("state", [])
        valid = (environ.get("PATH_INFO") == "/" and len(state) == 1
                 and secrets.compare_digest(state[0].encode("utf-8"), expected_state.encode("utf-8"))
                 and (len(params.get("code", [])) == 1 or len(params.get("error", [])) == 1))
        if valid:
            callback["uri"] = origin + "/?" + query
        start_response("200 OK" if valid else "400 Bad Request", [("Content-Type", "text/plain; charset=utf-8"), ("Cache-Control", "no-store")])
        return ["受信しました。PROLOAGの画面に戻ってください。".encode("utf-8") if valid else b"Invalid callback"]

    with make_server("127.0.0.1", 0, application, handler_class=_QuietHandler) as server:
        origin = f"http://127.0.0.1:{server.server_port}"
        flow.redirect_uri = origin + "/"
        authorization_url, expected_state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="false")
        parsed = urlsplit(authorization_url)
        if parsed.scheme != "https" or parsed.netloc != "accounts.google.com":
            raise GoogleShareError("認証URLを確認できませんでした。", code="oauth_failed")
        if not webbrowser.open(authorization_url, new=1, autoraise=True):
            raise GoogleShareError("認証用ブラウザを開けませんでした。", code="oauth_browser")
        deadline = time.monotonic() + min(max(timeout_seconds, 1), OAUTH_TIMEOUT)
        while not callback and time.monotonic() < deadline:
            server.timeout = min(1, max(deadline - time.monotonic(), 0.01))
            server.handle_request()
        if not callback:
            raise GoogleShareError("Google認証が時間内に完了しませんでした。", code="oauth_timeout")
        # OAuthlib validates state; HTTPS replacement matches the library's
        # installed-app strategy. The actual redirect_uri remains loopback HTTP.
        flow.fetch_token(authorization_response=callback["uri"].replace("http://", "https://", 1), timeout=HTTP_TIMEOUT)
        return flow.credentials


def _credentials_payload(credentials: Any) -> dict[str, Any]:
    try:
        payload = json.loads(credentials.to_json())
        if not isinstance(payload, dict) or payload.get("token_uri") != TOKEN_URI:
            raise ValueError("invalid credentials")
        if set(payload.get("scopes") or ()) != set(SCOPES) or not payload.get("refresh_token"):
            raise ValueError("invalid scope or offline credentials")
        return payload
    except Exception:
        raise GoogleShareError("必要最小権限の接続情報を取得できませんでした。", code="oauth_credentials") from None


def _authorized_transport(payload: dict[str, Any]):
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import AuthorizedSession
        if payload.get("token_uri") != TOKEN_URI or set(payload.get("scopes") or ()) != set(SCOPES):
            raise ValueError("invalid credentials")
        credentials = Credentials.from_authorized_user_info(payload, scopes=SCOPES)
        session = AuthorizedSession(credentials, refresh_timeout=HTTP_TIMEOUT, max_refresh_attempts=1)
        session.trust_env = False
        return session
    except ImportError:
        raise GoogleShareError("Google連携用の追加依存をインストールしてください。", code="dependencies_missing") from None
    except Exception:
        raise GoogleShareError("保存済み認証情報を利用できません。再接続してください。", code="oauth_credentials") from None


def _valid_saved_state(state: Any, root: Path) -> bool:
    return (isinstance(state, dict) and state.get("schema_version") == 1
            and state.get("project_key") == _project_key(root)
            and isinstance(state.get("credentials"), dict)
            and bool(re.fullmatch(r"[0-9a-f]{32}", str(state.get("connection_id", "")))))


def connection_status(root: Path) -> dict[str, Any]:
    result = {"connected": False, "dependencies_available": dependencies_available(), "supported": os.name == "nt",
              "spreadsheet_id": None, "url": None, "creation_pending": False,
              "docs": {}, "docs_creation_pending": {}}
    if not result["supported"]:
        return result
    try:
        state = _PrivateVault(root).read()
        if _valid_saved_state(state, root):
            result["connected"] = True
            book = state.get("book") or {}
            book_id = str(book.get("spreadsheet_id") or "")
            if _BOOK_ID.fullmatch(book_id):
                result.update(spreadsheet_id=book_id, url=f"https://docs.google.com/spreadsheets/d/{book_id}/edit")
            result["creation_pending"] = bool(state.get("creation_pending") or (book and book.get("status") != "ready"))
            for kind, entry in (state.get("docs") or {}).items():
                if kind in {"daily", "weekly"} and isinstance(entry, dict) and _BOOK_ID.fullmatch(str(entry.get("document_id", ""))):
                    document_id = entry["document_id"]
                    result["docs"][kind] = {"document_id": document_id,
                        "url": f"https://docs.google.com/document/d/{document_id}/edit", "status": entry.get("status")}
            result["docs_creation_pending"] = {kind: bool(value) for kind, value in (state.get("docs_creation_pending") or {}).items()
                                                if kind in {"daily", "weekly"}}
    except Exception:
        result["error_code"] = "private_storage"
    return result


def disconnect(root: Path) -> None:
    """Forget local authorization only. Remote files are never deleted/revoked."""
    _PrivateVault(root).delete()


class GoogleSheetsClient:
    def __init__(self, *, root: Path, transport: Any, vault: Any, state: dict[str, Any]):
        if not _valid_saved_state(state, root):
            raise GoogleShareError("接続情報がこのプロジェクトと一致しません。", code="connection_mismatch")
        self.root, self._transport, self._vault = Path(root).resolve(), transport, vault
        self._state = deepcopy(state)
        self._vault_revision = publication_hash(state)

    @classmethod
    def connect(cls, client_config: dict[str, Any], root: Path):
        config = _validate_client_config(client_config)
        vault = _PrivateVault(root)
        try:
            existing = vault.read()
            if (_valid_saved_state(existing, root) and (existing.get("book") or existing.get("creation_pending")
                                                     or existing.get("docs") or existing.get("docs_creation_pending"))
                    and existing.get("credentials", {}).get("client_id") != config["installed"]["client_id"]):
                raise GoogleShareError("別のOAuth設定へ変更する前に接続を解除してください。既存の共有先登録は保持しました。", code="oauth_client_mismatch")
            if not dependencies_available():
                raise GoogleShareError("Google連携用の追加依存をインストールしてください。", code="dependencies_missing")
            from google_auth_oauthlib.flow import InstalledAppFlow
            with _no_sensitive_logs():
                flow = InstalledAppFlow.from_client_config(config, scopes=SCOPES, autogenerate_code_verifier=True)
                flow.oauth2session.trust_env = False
                credentials = _run_oauth(flow)
                payload = _credentials_payload(credentials)
            # Preserve app-owned book identity across explicit reauthorization.
            same_client = (_valid_saved_state(existing, root)
                           and existing.get("credentials", {}).get("client_id") == payload.get("client_id"))
            state = {"schema_version": 1, "project_key": _project_key(root),
                     "connection_id": existing["connection_id"] if same_client else uuid4().hex,
                     "credentials": payload, "client_config": config}
            if same_client:
                for key in ("book", "creation_pending", "docs", "docs_creation_pending"):
                    if key in existing:
                        state[key] = existing[key]
            vault.write(state)
            return cls(root=root, transport=_authorized_transport(payload), vault=vault, state=state)
        except ImportError:
            raise GoogleShareError("Google連携用の追加依存をインストールしてください。", code="dependencies_missing") from None
        except GoogleShareError:
            raise
        except Exception:
            raise GoogleShareError("Google認証が完了しませんでした。設定または同意を確認してください。", code="oauth_failed") from None

    @classmethod
    def from_saved_connection(cls, root: Path):
        vault = _PrivateVault(root)
        state = vault.read()
        if not _valid_saved_state(state, root):
            raise GoogleShareError("このプロジェクトはGoogleへ接続されていません。", code="not_connected")
        if not dependencies_available():
            raise GoogleShareError("Google連携用の追加依存をインストールしてください。", code="dependencies_missing")
        return cls(root=root, transport=_authorized_transport(state["credentials"]), vault=vault, state=state)

    def disconnect(self) -> None:
        self._check_connection_current()
        self._vault.delete()
        self._state.clear()
        close = getattr(self._transport, "close", None)
        if close:
            try:
                close()
            except Exception:
                pass

    def _check_connection_current(self) -> None:
        if publication_hash(self._vault.read()) != self._vault_revision:
            raise GoogleShareError("別の操作でGoogle接続が更新されました。接続を読み直してから実行してください。", code="connection_stale")

    def _save(self) -> None:
        self._check_connection_current()
        credentials = getattr(self._transport, "credentials", None)
        if credentials is not None:
            self._state["credentials"] = _credentials_payload(credentials)
        self._vault.write(self._state)
        self._vault_revision = publication_hash(self._state)

    def _request(self, method: str, suffix: str = "", *, read_only: bool = False, _api_base: str = API_BASE, **kwargs: Any) -> dict[str, Any]:
        if _api_base not in {API_BASE, DOCS_API_BASE}:
            raise GoogleShareError("Google送信先が不正です。", code="invalid_destination")
        if not (suffix == "" or re.fullmatch(r"/[A-Za-z0-9_-]{5,160}(?::batchUpdate|:getByDataFilter)?", suffix)):
            raise GoogleShareError("Google送信先が不正です。", code="invalid_destination")
        self._check_connection_current()
        mutating = method != "GET" and not read_only
        try:
            with _no_sensitive_logs():
                response = self._transport.request(method, _api_base + suffix, timeout=HTTP_TIMEOUT,
                    max_allowed_time=HTTP_TIMEOUT, allow_redirects=False, **kwargs)
            status = response.status_code
            if not isinstance(status, int):
                raise ValueError("invalid response status")
        except Exception:
            raise GoogleShareError("Googleとの通信結果を確認できません。再送前に状態を確認してください。", code="transport", uncertain=mutating) from None
        if status < 200 or status >= 300:
            code = "reauthorize" if status in (401, 403) else "rate_limited" if status == 429 else "http_error"
            raise GoogleShareError("Googleへの操作が完了しませんでした。接続・権限・利用制限を確認してください。", code=code, uncertain=mutating and status >= 500) from None
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("invalid response")
            self._save()
            return result
        except Exception:
            raise GoogleShareError("Google応答または接続の保存を確認できませんでした。", code="response_unknown", uncertain=mutating) from None

    def _book(self, spreadsheet_id: str) -> dict[str, Any]:
        book = self._state.get("book")
        if (not isinstance(spreadsheet_id, str) or not _BOOK_ID.fullmatch(spreadsheet_id)
                or not isinstance(book, dict) or book.get("spreadsheet_id") != spreadsheet_id
                or book.get("sheet_ids") != SHEET_IDS):
            raise GoogleShareError("このアプリが作成・登録した共有先ではありません。", code="ownership")
        return book

    def _raw_metadata(self, spreadsheet_id: str) -> dict[str, Any]:
        self._book(spreadsheet_id)
        return self._request("GET", "/" + spreadsheet_id, params={
            "fields": "spreadsheetId,developerMetadata,sheets(properties,developerMetadata)",
            "includeGridData": "false",
        })

    def _owned_metadata(self, spreadsheet_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        self._book(spreadsheet_id)
        owner = f"1:{self._state['connection_id']}"
        markers = [m for m in raw.get("developerMetadata", []) if m.get("metadataKey") == "proloag.owner"]
        if raw.get("spreadsheetId") != spreadsheet_id or len(markers) != 1 or markers[0].get("metadataValue") != owner or markers[0].get("location") != {"spreadsheet": True}:
            raise GoogleShareError("共有先の所有情報が変更されています。自動更新を停止しました。", code="ownership")
        actual: dict[str, Any] = {}
        for sheet in raw.get("sheets", []):
            props = sheet.get("properties", {})
            marker = [m for m in sheet.get("developerMetadata", []) if m.get("metadataKey") == "proloag.role"]
            for role, sheet_id in SHEET_IDS.items():
                if props.get("sheetId") == sheet_id:
                    expected = f"{self._state['connection_id']}:{role}"
                    if (len(marker) != 1 or marker[0].get("metadataValue") != expected
                            or marker[0].get("location") != {"sheetId": sheet_id} or role in actual
                            or props.get("sheetType", "GRID") != "GRID"):
                        raise GoogleShareError("管理タブの所有情報が一致しません。更新を停止しました。", code="ownership")
                    actual[role] = props
            if marker and props.get("sheetId") not in SHEET_IDS.values():
                raise GoogleShareError("管理タブの複製または変更を検出しました。", code="ownership")
        if set(actual) != set(ROLES):
            raise GoogleShareError("管理タブが削除・変更されています。更新を停止しました。", code="ownership")
        return {"spreadsheet_id": spreadsheet_id, "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
                "owned": True, "connection_id": self._state["connection_id"], "sheets": dict(SHEET_IDS), "properties": actual}

    def read_metadata(self, spreadsheet_id: str) -> dict[str, Any]:
        try:
            return self._owned_metadata(spreadsheet_id, self._raw_metadata(spreadsheet_id))
        except GoogleShareError:
            raise
        except Exception:
            raise GoogleShareError("共有先の所有情報を確認できませんでした。", code="ownership") from None

    def create_spreadsheet(self, title: str) -> dict[str, Any]:
        book = self._state.get("book")
        if not book:
            if self._state.get("creation_pending"):
                raise GoogleShareError("前回の共有先作成結果が不明です。Driveで確認するまで自動再作成しません。", code="creation_unknown", uncertain=True)
            if not isinstance(title, str) or not title.strip() or len(title) > 100:
                raise GoogleShareError("共有先のタイトルが不正です。", code="invalid_title")
            self._state["creation_pending"] = True
            self._save()
            try:
                response = self._request("POST", json={"properties": {"title": title}, "sheets": [
                    {"properties": {"sheetId": SHEET_IDS[role], "title": SHEET_TITLES[role],
                                    "gridProperties": {"rowCount": 1000, "columnCount": 26}}} for role in ROLES]})
            except GoogleShareError as exc:
                if not exc.uncertain:
                    self._state["creation_pending"] = False
                    self._save()
                raise
            book_id = response.get("spreadsheetId")
            if not isinstance(book_id, str) or not _BOOK_ID.fullmatch(book_id):
                raise GoogleShareError("作成結果を特定できません。自動再作成を停止しました。", code="creation_unknown", uncertain=True)
            book = {"spreadsheet_id": book_id, "sheet_ids": dict(SHEET_IDS), "status": "initializing"}
            self._state.update(book=book, creation_pending=False)
            try:
                self._save()
            except GoogleShareError:
                raise GoogleShareError("共有先を作成しましたが登録の保存を確認できません。自動再作成を停止しました。", code="creation_unknown", uncertain=True) from None
        book_id = book["spreadsheet_id"]
        raw = self._raw_metadata(book_id)
        if book.get("status") != "ready":
            has_markers = bool(raw.get("developerMetadata")) or any(s.get("developerMetadata") for s in raw.get("sheets", []))
            if not has_markers:
                observed = {s.get("properties", {}).get("sheetId"): s.get("properties", {}).get("title") for s in raw.get("sheets", [])}
                if observed != {SHEET_IDS[role]: SHEET_TITLES[role] for role in ROLES}:
                    raise GoogleShareError("作成途中の共有先が変更されています。自動設定を停止しました。", code="ownership")
                markers = [{"metadataKey": "proloag.owner", "metadataValue": f"1:{self._state['connection_id']}", "location": {"spreadsheet": True}, "visibility": "PROJECT"}]
                markers += [{"metadataKey": "proloag.role", "metadataValue": f"{self._state['connection_id']}:{role}",
                             "location": {"sheetId": SHEET_IDS[role]}, "visibility": "PROJECT"} for role in ROLES]
                self._request("POST", "/" + book_id + ":batchUpdate", json={"requests": [
                    {"createDeveloperMetadata": {"developerMetadata": marker}} for marker in markers]})
                raw = self._raw_metadata(book_id)
            self._owned_metadata(book_id, raw)
            book["status"] = "ready"
            self._save()
        metadata = self._owned_metadata(book_id, raw)
        return {"spreadsheet_id": book_id, "url": metadata["url"]}

    @staticmethod
    def _cell(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, bool):
            return {"userEnteredValue": {"boolValue": value}}
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if isinstance(value, int) and abs(value) > 2 ** 53:
                return {"userEnteredValue": {"stringValue": str(value)}}
            if not math.isfinite(value):
                raise GoogleShareError("有限でない数値は送信できません。", code="invalid_snapshot")
            return {"userEnteredValue": {"numberValue": value}}
        if not isinstance(value, str) or len(value) > 40000:
            raise GoogleShareError("送信セルの型または長さが上限を超えています。", code="invalid_snapshot")
        return {"userEnteredValue": {"stringValue": value}}

    def publish(self, spreadsheet_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        if (not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1
                or not _HEX_ID.fullmatch(str(snapshot.get("snapshot_id", "")))
                or not isinstance(snapshot.get("tables"), dict)
                or not set(snapshot["tables"]).issubset(set(ROLES) - {"manifest"})):
            raise GoogleShareError("送信スナップショットの形式が不正です。", code="invalid_snapshot")
        book = self._book(spreadsheet_id)
        if book.get("status") != "ready":
            raise GoogleShareError("共有先の初期設定が完了していません。", code="ownership")
        payload_hash = publication_hash(snapshot)
        metadata = self.read_metadata(spreadsheet_id)
        tables = deepcopy(snapshot["tables"])
        manifest = {key: snapshot.get(key) for key in MANIFEST_COLUMNS}
        manifest["payload_hash"] = payload_hash
        tables["manifest"] = [list(MANIFEST_COLUMNS), [
            canonical_json(manifest[key]).decode("utf-8") if isinstance(manifest[key], (dict, list)) else manifest[key]
            for key in MANIFEST_COLUMNS]]
        requests = []
        for role in ROLES:  # Manifest is deliberately last in the same atomic batch.
            rows = tables.get(role, [])
            if not isinstance(rows, list) or len(rows) > 100000 or any(not isinstance(row, list) or len(row) > 128 for row in rows):
                raise GoogleShareError("送信表のサイズまたは形式が不正です。", code="invalid_snapshot")
            grid = metadata["properties"][role].get("gridProperties", {})
            if (not isinstance(grid, dict) or any(type(grid.get(key)) is not int or grid[key] < 1
                                                 for key in ("rowCount", "columnCount"))):
                raise GoogleShareError("管理タブのサイズ情報を確認できません。", code="ownership")
            height = max(grid["rowCount"], len(rows), 1)
            width = max(grid["columnCount"], max(map(len, rows), default=1), 1)
            if height * width > 2_000_000:
                raise GoogleShareError("管理タブのサイズが送信上限を超えています。", code="payload_too_large")
            if height > grid.get("rowCount", 0) or width > grid.get("columnCount", 0):
                requests.append({"updateSheetProperties": {"properties": {"sheetId": SHEET_IDS[role],
                    "gridProperties": {"rowCount": height, "columnCount": width}}, "fields": "gridProperties.rowCount,gridProperties.columnCount"}})
            requests.append({"updateCells": {"range": {"sheetId": SHEET_IDS[role], "startRowIndex": 0,
                "endRowIndex": height, "startColumnIndex": 0, "endColumnIndex": width},
                "rows": [{"values": [self._cell(value) for value in row]} for row in rows], "fields": "userEnteredValue"}})
        body = {"requests": requests}
        if len(canonical_json(body)) > MAX_REQUEST_BYTES:
            raise GoogleShareError("一括送信サイズが上限を超えています。期間や明細対象を減らしてください。", code="payload_too_large")
        self._request("POST", "/" + spreadsheet_id + ":batchUpdate", json=body)
        return {"snapshot_id": snapshot["snapshot_id"], "payload_hash": payload_hash, "spreadsheet_id": spreadsheet_id}

    def read_publication(self, spreadsheet_id: str) -> dict[str, Any]:
        self.read_metadata(spreadsheet_id)
        raw = self._request("POST", "/" + spreadsheet_id + ":getByDataFilter", read_only=True, json={
            "dataFilters": [{"gridRange": {"sheetId": SHEET_IDS["manifest"], "startRowIndex": 0,
                "endRowIndex": 2, "startColumnIndex": 0, "endColumnIndex": len(MANIFEST_COLUMNS)}}], "includeGridData": True})
        try:
            sheets = [sheet for sheet in raw.get("sheets", []) if sheet.get("properties", {}).get("sheetId") == SHEET_IDS["manifest"]]
            if len(sheets) != 1:
                raise ValueError("missing manifest")
            row_data = (sheets[0].get("data") or [{}])[0].get("rowData", [])
            if not row_data or not any(row.get("values") for row in row_data):
                return {}
            rows = []
            for row in row_data[:2]:
                cells = []
                for cell in row.get("values", []):
                    value = cell.get("userEnteredValue", {})
                    if len(value) > 1 or set(value) - {"stringValue", "numberValue", "boolValue"}:
                        raise ValueError("formula in manifest")
                    cells.append(next(iter(value.values()), None))
                rows.append(cells)
            if len(rows) != 2 or rows[0] != list(MANIFEST_COLUMNS) or len(rows[1]) != len(MANIFEST_COLUMNS):
                raise ValueError("invalid manifest")
            result = dict(zip(MANIFEST_COLUMNS, rows[1]))
            if not _HEX_ID.fullmatch(str(result["snapshot_id"])) or not _HEX_ID.fullmatch(str(result["payload_hash"])) or result["schema_version"] != 1:
                raise ValueError("invalid manifest")
            for key in ("period", "options", "counts"):
                result[key] = json.loads(result[key])
            return result
        except Exception:
            raise GoogleShareError("共有先の公開記録を確認できません。自動再送を停止しました。", code="invalid_publication") from None
