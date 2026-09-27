import json
from urllib.parse import quote, urlparse

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials

FOLDER_MIME = "application/vnd.google-apps.folder"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"


class DriveStorage:
    def __init__(self, credentials_json: str, root_folder_id: str):
        info = json.loads(credentials_json)
        credentials = Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/drive.file"])
        self.credentials = credentials
        self.root_folder_id = root_folder_id

    @staticmethod
    def _request(session: AuthorizedSession, method: str, url: str, **kwargs):
        response = session.request(method, url, timeout=120, allow_redirects=False, **kwargs)
        if not 200 <= response.status_code < 300:
            # Do not expose response bodies, document metadata, or upload session URLs.
            raise RuntimeError(f"Google Drive request failed (HTTP {response.status_code})")
        return response

    def _folder(self, session: AuthorizedSession, name: str, parent_id: str) -> str:
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        safe_parent = parent_id.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name = '{safe}' and mimeType = '{FOLDER_MIME}' and '{safe_parent}' in parents and trashed = false"
        result = self._request(session, "GET", FILES_URL, params={
            "q": query, "spaces": "drive", "fields": "files(id,name)", "pageSize": 1,
        }).json()
        if result.get("files"):
            return result["files"][0]["id"]
        created = self._request(session, "POST", FILES_URL, params={"fields": "id"},
                                json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}).json()
        return created["id"]

    def upload(self, content: bytes, filename: str, mime_type: str, folders: list[str]) -> dict[str, str]:
        with AuthorizedSession(self.credentials) as session:
            parent = self.root_folder_id
            for folder in folders:
                parent = self._folder(session, folder, parent)
            # Create a new file every time, even when its display name already exists.
            # A resumable session also supports uploads larger than 5 MB.
            response = self._request(session, "POST", UPLOAD_URL,
                                     params={"uploadType": "resumable", "fields": "id"},
                                     json={"name": filename, "parents": [parent]},
                                     headers={"X-Upload-Content-Type": mime_type,
                                              "X-Upload-Content-Length": str(len(content))})
            upload_url = response.headers.get("Location", "")
            parsed = urlparse(upload_url)
            if (parsed.scheme != "https" or parsed.netloc != "www.googleapis.com"
                    or not parsed.path.startswith("/upload/drive/v3/files") or parsed.fragment):
                raise RuntimeError("Google Drive returned an invalid upload URL")
            item = self._request(session, "PUT", upload_url, data=content,
                                 headers={"Content-Type": mime_type}).json()
        # Construct the only external URL returned to the client from the
        # opaque Drive file ID rather than trusting response-provided URLs.
        return {"file_id": item["id"], "web_url": f"https://drive.google.com/file/d/{item['id']}/view"}

    def download(self, file_id: str) -> bytes:
        with AuthorizedSession(self.credentials) as session:
            return self._request(session, "GET", f"{FILES_URL}/{quote(file_id, safe='')}",
                                 params={"alt": "media"}).content
