"""업로드 파일 저장소.

- 로컬: instance/uploads/<key>
- Supabase Storage: SUPABASE_URL + SUPABASE_SERVICE_KEY 가 설정되면 비공개 버킷에 저장
  (Vercel 처럼 파일을 보관할 수 없는 서버리스 환경용). 파일은 항상 앱이 로그인 확인 후 전달한다.
"""
import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote

from flask import current_app


class StorageError(RuntimeError):
    pass


class LocalStore:
    kind = "local"

    def __init__(self, root):
        self.root = os.path.realpath(root)

    def _path(self, key):
        p = os.path.realpath(os.path.join(self.root, key))
        if not p.startswith(self.root + os.sep):  # 경로 조작 방지
            raise StorageError("잘못된 경로")
        return p

    def save(self, key, data: bytes, content_type="application/octet-stream"):
        p = self._path(key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    def read(self, key):
        try:
            with open(self._path(key), "rb") as f:
                return f.read()
        except (OSError, StorageError):
            return None

    def delete(self, key):
        try:
            os.remove(self._path(key))
        except (OSError, StorageError):
            pass


class SupabaseStore:
    kind = "supabase"

    def __init__(self, url, key, bucket):
        self.base = url.rstrip("/") + "/storage/v1"
        self.key = key
        self.bucket = bucket

    def _req(self, method, path, data=None, headers=None, timeout=20):
        h = {"Authorization": f"Bearer {self.key}", "apikey": self.key}
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except urllib.error.URLError as e:
            raise StorageError(f"저장소에 연결할 수 없습니다: {e.reason}") from None

    def _obj(self, key):
        return f"/object/{self.bucket}/{quote(key)}"

    def save(self, key, data: bytes, content_type="application/octet-stream"):
        st, body = self._req("POST", self._obj(key), data,
                             {"Content-Type": content_type, "x-upsert": "true"})
        if st >= 300:
            raise StorageError(f"파일 저장 실패 ({st}): {body[:200]!r}")

    def read(self, key):
        st, body = self._req("GET", self._obj(key))
        return body if st == 200 else None

    def delete(self, key):
        try:
            self._req("DELETE", self._obj(key))
        except StorageError:
            pass

    def ensure_bucket(self):
        st, body = self._req("POST", "/bucket", json.dumps(
            {"id": self.bucket, "name": self.bucket, "public": False}).encode(),
            {"Content-Type": "application/json"})
        if st < 300:
            return "created"
        if st in (400, 409) and b"exist" in body.lower():
            return "exists"
        raise StorageError(f"버킷 생성 실패 ({st}): {body[:200]!r}")


def get_store():
    c = current_app.config
    if c.get("SUPABASE_URL") and c.get("SUPABASE_SERVICE_KEY"):
        return SupabaseStore(c["SUPABASE_URL"], c["SUPABASE_SERVICE_KEY"], c.get("STORAGE_BUCKET", "uploads"))
    return LocalStore(os.path.join(current_app.instance_path, "uploads"))
