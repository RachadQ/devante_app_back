import json
from io import BytesIO

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveStorage:
    def __init__(self, credentials_json: str, root_folder_id: str):
        info = json.loads(credentials_json)
        credentials = Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/drive.file"])
        self.service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self.root_folder_id = root_folder_id

    def _folder(self, name: str, parent_id: str) -> str:
        safe = name.replace("'", "\\'")
        query = f"name = '{safe}' and mimeType = '{FOLDER_MIME}' and '{parent_id}' in parents and trashed = false"
        result = self.service.files().list(q=query, spaces="drive", fields="files(id,name)", pageSize=1).execute()
        if result.get("files"):
            return result["files"][0]["id"]
        created = self.service.files().create(body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}, fields="id").execute()
        return created["id"]

    def upload(self, content: bytes, filename: str, mime_type: str, folders: list[str]) -> dict[str, str]:
        parent = self.root_folder_id
        for folder in folders:
            parent = self._folder(folder, parent)
        media = MediaIoBaseUpload(BytesIO(content), mimetype=mime_type, resumable=False)
        item = self.service.files().create(body={"name": filename, "parents": [parent]}, media_body=media, fields="id,webViewLink").execute()
        # Construct the only external URL returned to the client from the
        # opaque Drive file ID rather than trusting response-provided URLs.
        return {"file_id": item["id"], "web_url": f"https://drive.google.com/file/d/{item['id']}/view"}

    def download(self, file_id: str) -> bytes:
        return self.service.files().get_media(fileId=file_id).execute()
