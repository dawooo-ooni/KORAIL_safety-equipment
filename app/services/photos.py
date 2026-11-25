"""장비 사진 저장·축소·삭제 (작업지시서 8.5).
EXIF 회전 보정 → 긴 변 1280px → JPEG 품질 80. 원본은 저장하지 않는다.
저장 위치는 services/storage.py (로컬 instance/uploads/photos/ 또는 Supabase Storage)."""
import io
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError

from .storage import StorageError, get_store

try:  # HEIC(아이폰 사진)는 pillow-heif 가 설치돼 있을 때만 지원
    from pillow_heif import register_heif_opener  # type: ignore

    register_heif_opener()
    HEIC_OK = True
except Exception:  # noqa: BLE001
    HEIC_OK = False

ALLOWED_EXT = {"jpg", "jpeg", "png", "heic", "heif", "webp", "gif", "bmp"}
MAX_SIDE = 1280
QUALITY = 80
PREFIX = "photos/"


class PhotoError(ValueError):
    pass


def _key(rel):
    if not rel or "/" in rel or "\\" in rel or ".." in rel:
        raise PhotoError("잘못된 경로")
    return PREFIX + rel


def save_upload(file_storage):
    """업로드 파일을 검증·축소해 저장. 반환: (상대경로, width, height, size_bytes)"""
    if not file_storage or not file_storage.filename:
        raise PhotoError("사진 파일을 선택하세요.")
    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in ALLOWED_EXT:
        raise PhotoError("jpg·png·heic 이미지 파일만 올릴 수 있습니다.")
    if ext in ("heic", "heif") and not HEIC_OK:
        raise PhotoError("이 서버는 HEIC 사진을 처리할 수 없습니다. JPG로 바꿔 올리거나 관리자에게 pillow-heif 설치를 요청하세요.")
    try:
        img = Image.open(file_storage.stream)
        img.load()
    except (UnidentifiedImageError, OSError, ValueError):
        raise PhotoError("이미지 파일이 아니거나 손상된 파일입니다.") from None
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=QUALITY, optimize=True)
    data = buf.getvalue()
    name = uuid.uuid4().hex + ".jpg"
    try:
        get_store().save(_key(name), data, "image/jpeg")
    except StorageError as e:
        raise PhotoError(str(e)) from None
    return name, img.width, img.height, len(data)


def read_file(rel):
    try:
        return get_store().read(_key(rel))
    except (PhotoError, StorageError):
        return None


def remove_file(rel):
    try:
        get_store().delete(_key(rel))
    except (PhotoError, StorageError):
        pass
