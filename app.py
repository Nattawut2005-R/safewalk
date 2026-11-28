import base64
import hashlib
import hmac
import os
import time
import uuid
import boto3
import requests
from flask import Flask, jsonify, request
from supabase import Client, create_client

app = Flask(__name__)

# --- Environment Variables ---
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY")
    or os.environ.get("SUPABASE_ANON_KEY")
    or os.environ.get("SUPABASE_KEY", "")
)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "safewalk-images")
R2_PUBLIC_BASE_URL = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")

DEFAULT_LAT = 13.7138
DEFAULT_LNG = 100.5364

CATEGORY_MAP = {
    "ควันบุหรี่": "smoke",
    "สูบบุหรี่ในพื้นที่ห้ามสูบ": "smoke",
    "บุหรี่": "smoke",
    "smoke": "smoke",
    "ไฟทางดับ": "dark",
    "ไฟทางดับ / ทางเดินมืด": "dark",
    "ทางมืด": "dark",
    "dark": "dark",
    "กีดขวาง": "block",
    "ขายของปิดถนน / ตั้งเต็นท์": "block",
    "ปิดถนน": "block",
    "ตั้งเต็นท์": "block",
    "block": "block",
    "อื่นๆ": "other",
    "other": "other",
}

CATEGORY_THAI_LABEL = {
    "smoke": "สูบบุหรี่ในพื้นที่ห้ามสูบ",
    "dark": "ไฟทางดับ / ทางเดินมืด",
    "block": "ขายของปิดถนน / ตั้งเต็นท์",
    "other": "อื่นๆ",
}

supabase: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

s3_r2 = None
if R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY:
    s3_r2 = boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )

# ลำดับ step ต่อ user_id:
# 1. "step_photo"    -> รอรูป หรือ กดไม่แนบรูป
# 2. "step_category" -> รอเลือกเหตุการณ์ (ควันบุหรี่ / ไฟทางดับ / กีดขวาง / อื่นๆ)
# 3. "step_location" -> รอกดปุ่มปักหมุด Location (หรือพิมพ์รายละเอียดเพิ่มก่อนปักหมุด)
# 4. "done"          -> ปักหมุดและขอบคุณเรียบร้อย (เริ่มรอบใหม่เมื่อทักมาอีกครั้ง)
sessions: dict[str, dict] = {}


def generate_hazard_id() -> str:
    return f"RMUTK-{uuid.uuid4().hex[:6].upper()}"


def new_session() -> dict:
    return {
        "step": "step_photo",
        "category": None,
        "description": "",
        "lat": None,
        "lng": None,
        "image_url": None,
    }


def get_session(user_id: str) -> dict:
    if user_id not in sessions:
        sessions[user_id] = new_session()
    return sessions[user_id]


def verify_line_signature(body_bytes: bytes, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET:
        return True
    if not signature:
        return False
    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body_bytes,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def send_line_message(reply_token: str, text: str, quick_items: list[dict] | None = None) -> None:
    if not reply_token or not LINE_CHANNEL_ACCESS_TOKEN:
        return
    message_obj: dict = {"type": "text", "text": text}
    if quick_items:
        message_obj["quickReply"] = {"items": quick_items}
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            },
            json={
                "replyToken": reply_token,
                "messages": [message_obj],
            },
            timeout=10,
        )
        if resp.status_code >= 400:
            print("[LINE reply HTTP error]:", resp.status_code, resp.text)
    except Exception as exc:
        print("[LINE reply error]:", exc)


# --- Quick Reply แยกตามแต่ละขั้น ---
def qr_photo() -> list[dict]:
    return [
        {"type": "action", "action": {"type": "camera", "label": "ถ่ายรูป"}},
        {"type": "action", "action": {"type": "cameraRoll", "label": "แนบรูปจากอัลบั้ม"}},
        {"type": "action", "action": {"type": "message", "label": "ไม่แนบรูป", "text": "ไม่แนบรูป"}},
    ]


def qr_category() -> list[dict]:
    labels = ["ควันบุหรี่", "ไฟทางดับ", "กีดขวาง", "อื่นๆ"]
    return [
        {"type": "action", "action": {"type": "message", "label": lbl, "text": lbl}}
        for lbl in labels
    ]


def qr_location() -> list[dict]:
    return [
        {"type": "action", "action": {"type": "location", "label": "📍 ปักหมุดตำแหน่ง"}},
    ]


def qr_restart() -> list[dict]:
    return [
        {"type": "action", "action": {"type": "message", "label": "แจ้งเหตุเพิ่มเติม", "text": "เริ่มแจ้งเหตุ"}},
    ]


def upload_line_image_to_r2(message_id: str) -> str:
    if not s3_r2 or not R2_PUBLIC_BASE_URL:
        raise RuntimeError("ยังไม่ได้ตั้งค่า R2 ใน Render")

    content_url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    resp = requests.get(content_url, headers=headers, timeout=20)
    resp.raise_for_status()

    content_type = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
    ext = "jpg"
    if "png" in content_type:
        ext = "png"
    elif "webp" in content_type:
        ext = "webp"
    elif "gif" in content_type:
        ext = "gif"

    key = f"hazards/line-{int(time.time())}-{uuid.uuid4().hex[:8]}.{ext}"
    s3_r2.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=resp.content,
        ContentType=content_type,
    )
    return f"{R2_PUBLIC_BASE_URL}/{key}"


def insert_hazard(session: dict) -> str:
    if not supabase:
        raise RuntimeError("ยังไม่ได้ตั้งค่า SUPABASE_URL หรือ SUPABASE_KEY")

    hazard_id = generate_hazard_id()
    lat = float(session["lat"]) if session.get("lat") is not None else DEFAULT_LAT
    lng = float(session["lng"]) if session.get("lng") is not None else DEFAULT_LNG
    cat = session.get("category") or "other"
    desc = (session.get("description") or "").strip() or CATEGORY_THAI_LABEL.get(cat, "แจ้งจุดเสี่ยง")

    payload = {
        "id": hazard_id,
        "category": cat,
        "description": desc,
        "lat": lat,
        "lng": lng,
        "image_url": session.get("image_url"),
        "is_resolved": False,
        "resolved_votes": 0,
        "unresolved_votes": 0,
    }
    res = supabase.table("hazards").insert(payload).execute()
    if getattr(res, "error", None):
        raise RuntimeError(str(res.error))
    return hazard_id


def ask_step_photo(reply_token: str) -> None:
    send_line_message(
        reply_token,
        "คุณสามารถแจ้งเหตุที่พบ ใน มทร.กรุงเทพ ได้เลยครับ\nสามารถส่งรูปภาพเข้ามา หรือเลือก \"ไม่แนบรูป\" ได้ด้านล่างครับ",
        quick_items=qr_photo(),
    )


def ask_step_category(reply_token: str, prefix: str = "") -> None:
    msg = (f"{prefix}\n" if prefix else "") + "ขั้นต่อไป: กรุณาเลือกเรื่อง/เหตุการณ์ที่ต้องการแจ้งครับ 👇"
    send_line_message(reply_token, msg, quick_items=qr_category())


def ask_step_location(reply_token: str, cat_label: str) -> None:
    send_line_message(
        reply_token,
        f"เลือกเรื่อง \"{cat_label}\" เรียบร้อยครับ\nขั้นสุดท้าย: กดปุ่ม \"📍 ปักหมุดตำแหน่ง\" ด้านล่างเพื่อส่งจุดเกิดเหตุ (หรือพิมพ์รายละเอียดเพิ่มเติมก่อนปักหมุดได้เลยครับ)",
        quick_items=qr_location(),
    )


def process_event(event: dict) -> None:
    if event.get("type") != "message":
        return

    user_id = (event.get("source") or {}).get("userId")
    if not user_id:
        return

    reply_token = event.get("replyToken", "")
    msg = event.get("message") or {}
    msg_type = msg.get("type")
    sess = get_session(user_id)

    if sess.get("step") == "done":
        sessions[user_id] = new_session()
        sess = sessions[user_id]

    if msg_type == "text":
        raw_text = (msg.get("text") or "").strip()
        if raw_text in ("เริ่มใหม่", "ยกเลิก", "เริ่มแจ้งเหตุ", "แจ้งเหตุ"):
            sessions[user_id] = new_session()
            ask_step_photo(reply_token)
            return

    step = sess.get("step")

    # ==========================================
    # ขั้นที่ 1: รอรูปภาพ หรือ ไม่แนบรูป -> ไปขั้นเลือกเหตุการณ์
    # ==========================================
    if step == "step_photo":
        if msg_type == "image":
            try:
                image_url = upload_line_image_to_r2(msg.get("id", ""))
                sess["image_url"] = image_url
                sess["step"] = "step_category"
                ask_step_category(reply_token, "บันทึกรูปภาพเรียบร้อยแล้วครับ ✅")
            except Exception as exc:
                print("[R2 upload error]:", exc)
                sess["image_url"] = None
                sess["step"] = "step_category"
                ask_step_category(reply_token, "รับทราบครับ (ข้ามการแนบรูป)")
            return

        if msg_type == "text":
            text = (msg.get("text") or "").strip()
            if text in ("ไม่แนบรูป", "ข้าม", "ไม่ส่งรูป"):
                sess["image_url"] = None
                sess["step"] = "step_category"
                ask_step_category(reply_token, "รับทราบครับ ไม่แนบรูปภาพ")
                return

        ask_step_photo(reply_token)
        return

    # ==========================================
    # ขั้นที่ 2: รอเลือกเหตุการณ์ -> ไปขั้นปักหมุด Location
    # ==========================================
    if step == "step_category":
        if msg_type == "text":
            text = (msg.get("text") or "").strip()
            lower = text.lower()
            if lower in CATEGORY_MAP:
                cat_key = CATEGORY_MAP[lower]
                sess["category"] = cat_key
                sess["step"] = "step_location"
                ask_step_location(reply_token, CATEGORY_THAI_LABEL[cat_key])
                return

        send_line_message(
            reply_token,
            "กรุณากดเลือกประเภทเหตุการณ์จากปุ่มด้านล่างก่อนครับ 👇",
            quick_items=qr_category(),
        )
        return

    # ==========================================
    # ขั้นที่ 3: รอปักหมุด Location -> บันทึก & ขึ้นขอบคุณ + รหัส RMUTK-XXXXXX
    # ==========================================
    if step == "step_location":
        if msg_type == "location":
            sess["lat"] = msg.get("latitude", DEFAULT_LAT)
            sess["lng"] = msg.get("longitude", DEFAULT_LNG)
            addr = (msg.get("address") or "").strip()
            if addr and not sess.get("description"):
                sess["description"] = addr

            try:
                hazard_id = insert_hazard(sess)
                cat_label = CATEGORY_THAI_LABEL.get(sess.get("category") or "other", "อื่นๆ")
                sess["step"] = "done"
                send_line_message(
                    reply_token,
                    f"ขอบคุณที่ร่วมแจ้งเหตุเพื่อความปลอดภัยใน มทร.กรุงเทพ ครับ 🙏✅\n\n"
                    f"รหัสแจ้งเหตุ: {hazard_id}\n"
                    f"เรื่องที่แจ้ง: {cat_label}\n"
                    f"พิกัด: {float(sess['lat']):.5f}, {float(sess['lng']):.5f}\n\n"
                    f"ระบบได้ปักหมุดลงแผนที่เรียบร้อยแล้วครับ",
                    quick_items=qr_restart(),
                )
            except Exception as exc:
                print("[Supabase save error]:", exc)
                send_line_message(
                    reply_token,
                    f"บันทึกข้อมูลไม่สำเร็จ: {exc}\nกรุณาลองกดปักหมุดอีกครั้งครับ",
                    quick_items=qr_location(),
                )
            return

        if msg_type == "text":
            detail = (msg.get("text") or "").strip()
            if detail:
                sess["description"] = detail
                send_line_message(
                    reply_token,
                    f"บันทึกรายละเอียด \"{detail}\" แล้วครับ\nต่อไปกดปุ่ม \"📍 ปักหมุดตำแหน่ง\" ด้านล่างเพื่อส่งพิกัดและจบการแจ้งเหตุได้เลยครับ 👇",
                    quick_items=qr_location(),
                )
                return

        send_line_message(
            reply_token,
            "กรุณากดปุ่ม \"📍 ปักหมุดตำแหน่ง\" ด้านล่างเพื่อปักหมุดจุดเกิดเหตุครับ 👇",
            quick_items=qr_location(),
        )


@app.get("/")
def home():
    return "RMUTK SafeWalk Bot is running", 200


@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.post("/webhook")
@app.post("/webhook/line")
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    raw_body = request.get_data()

    if not verify_line_signature(raw_body, signature):
        print("[LINE signature invalid]")
        return jsonify({"error": "Invalid signature"}), 401

    payload = request.get_json(silent=True) or {}
    events = payload.get("events", [])
    print(f"[LINE webhook received] events={len(events)}")
    for event in events:
        try:
            process_event(event)
        except Exception as exc:
            print("[process_event error]:", exc)

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
