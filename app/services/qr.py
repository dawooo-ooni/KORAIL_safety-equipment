"""QR 코드 생성. QR 에는 고정 주소 {APP_BASE_URL}/q/{관리번호} 만 담는다 (4.6)."""
import io
from urllib.parse import quote

import qrcode
from flask import current_app
from qrcode.constants import ERROR_CORRECT_M


def qr_url(mgmt_no):
    return f"{current_app.config['APP_BASE_URL']}/q/{quote(mgmt_no)}"


def qr_png(text, box_size=8, border=2):
    q = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=box_size, border=border)
    q.add_data(text)
    q.make(fit=True)
    img = q.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
