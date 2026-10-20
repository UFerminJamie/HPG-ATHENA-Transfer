"""Small Girder REST client for the endpoints in Athena's live /api/v1/describe."""

import json
from typing import Any, Dict, Iterator, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .matching import Slide, parse_destination, slide_basename


class APIError(RuntimeError):
    """A deliberately credential-free error for console and manifest output."""


class AthenaClient:
    def __init__(self, api_url: str, api_key: str):
        if not api_url.startswith("https://"):
            raise ValueError("Athena API URL must use HTTPS")
        if not api_key:
            raise ValueError("ATHENA_API_KEY is required")
        self.api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._token: Optional[str] = None

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                 body: Optional[Any] = None, authenticated: bool = True) -> Any:
        query = "?" + urlencode(params) if params else ""
        url = self.api_url + path + query
        headers = {"Accept": "application/json"}
        if authenticated:
            self.authenticate()
            headers["Girder-Token"] = self._token or ""
        data = None
        if body is not None:
            data = json.dumps(body, allow_nan=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif method == "POST":
            data = b""
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=300) as response:
                result = response.read()
            return json.loads(result) if result else None
        except HTTPError as exc:
            # Do not include URL, response body, or exception text: those could
            # contain the API key or server-side request details.
            raise APIError("Athena HTTP %d on %s %s" % (exc.code, method, path)) from None
        except (URLError, TimeoutError, OSError) as exc:
            raise APIError("Athena connection failed on %s %s (%s)" % (method, path, type(exc).__name__)) from None
        except (ValueError, UnicodeDecodeError):
            raise APIError("Athena returned an invalid JSON response on %s %s" % (method, path)) from None

    def authenticate(self) -> None:
        if self._token:
            return
        data = self._request("POST", "/api_key/token", {"key": self._api_key}, authenticated=False)
        token = (data.get("authToken") or {}).get("token") if isinstance(data, dict) else None
        if not token and isinstance(data, dict):
            token = data.get("token")
        if not isinstance(token, str) or not token:
            raise APIError("Athena did not return an authentication token")
        self._token = token

    def check_connection(self) -> Dict[str, Any]:
        user = self._request("GET", "/user/me")
        if not isinstance(user, dict) or not user.get("_id"):
            raise APIError("Athena authentication did not identify a user")
        return {"id": user["_id"], "login": user.get("login", "")}

    def paged(self, path: str, params: Optional[Dict[str, Any]] = None) -> Iterator[Dict[str, Any]]:
        offset = 0
        limit = 100
        while True:
            page = self._request("GET", path, dict(params or {}, limit=limit, offset=offset))
            if not isinstance(page, list):
                raise APIError("Athena returned a non-list page for " + path)
            for record in page:
                if not isinstance(record, dict):
                    raise APIError("Athena returned a malformed record for " + path)
                yield record
            if len(page) < limit:
                break
            offset += len(page)

    def resolve_destination(self, value: str) -> tuple:
        resource_type, object_id = parse_destination(value)
        if resource_type:
            result = self._request("GET", "/%s/%s" % (resource_type, object_id))
            if not isinstance(result, dict) or result.get("_id") != object_id:
                raise APIError("Destination could not be confirmed")
            return resource_type, object_id
        found: List[str] = []
        for candidate in ("folder", "collection"):
            try:
                result = self._request("GET", "/%s/%s" % (candidate, object_id))
                if isinstance(result, dict) and result.get("_id") == object_id:
                    found.append(candidate)
            except APIError as exc:
                if "HTTP 400" not in str(exc) and "HTTP 404" not in str(exc):
                    raise
        if len(found) != 1:
            raise APIError("Bare destination ID could not be resolved uniquely; use a collection or folder URL")
        return found[0], object_id

    def slides(self, destination: str) -> List[Slide]:
        parent_type, parent_id = self.resolve_destination(destination)
        queue = [(parent_type, parent_id)]
        seen_folders = set()
        slides: List[Slide] = []
        seen_items = set()
        while queue:
            parent_type, parent_id = queue.pop(0)
            if parent_type == "folder":
                if parent_id in seen_folders:
                    continue
                seen_folders.add(parent_id)
                for item in self.paged("/item", {"folderId": parent_id}):
                    item_id = item.get("_id")
                    if not isinstance(item_id, str) or item_id in seen_items:
                        continue
                    seen_items.add(item_id)
                    names = set()
                    item_name = item.get("name", "")
                    large_image = item.get("largeImage")
                    large_image_file_id = large_image.get("fileId") if isinstance(large_image, dict) else None
                    if isinstance(item_name, str):
                        name = slide_basename(item_name, allow_bare=bool(large_image),
                                              known_large_image=bool(large_image))
                        if name:
                            names.add(name)
                    for file in self.paged("/item/%s/files" % item_id):
                        filename = file.get("name", "")
                        if isinstance(filename, str):
                            name = slide_basename(filename, known_large_image=bool(
                                large_image_file_id and file.get("_id") == large_image_file_id))
                            if name:
                                names.add(name)
                    if names:
                        slides.append(Slide(item_id, item_name, tuple(sorted(names))))
            for folder in self.paged("/folder", {"parentType": parent_type, "parentId": parent_id}):
                folder_id = folder.get("_id")
                if isinstance(folder_id, str):
                    queue.append(("folder", folder_id))
        return slides

    def annotations(self, item_id: str) -> List[Dict[str, Any]]:
        return list(self.paged("/annotation", {"itemId": item_id}))

    def create_annotation(self, item_id: str, document: Dict[str, Any]) -> Dict[str, Any]:
        result = self._request("POST", "/annotation", {"itemId": item_id}, document)
        if not isinstance(result, dict) or not result.get("_id"):
            raise APIError("Athena did not return an annotation ID")
        return result
