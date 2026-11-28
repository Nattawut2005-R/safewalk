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

# สถานะการแจ้งเหตุต่อ user_id:
# step: "intro_photo" -> "waiting_details" -> "done"
sessions: dict[str, dict] = {}


def generate_hazard_id() -> str:
    return f"RMUTK-{uuid.uuid4().hex[:6].upper()}"


def get_session(user_id: str) -> dict:
    if user_id not in sessions:
        sessions[user_id] = {
            "step": "intro_photo",
            "category": "other",
            "description": "",
            "lat": None,
            "lng": None,
            "image_url": None,
            "created_id": None,
        }
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
        requests.post(
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
    except Exception as exc:
        print("[LINE reply error]:", exc)


def photo_choice_quick_reply() -> list[dict]:
    return [
        {
            "type": "action",
            "action": {
                "type": "cameraRoll",
                "label": "แนบรูป",
            },
        },
        {
            "type": "action",
            "action": {
                "type": "message",
                "label": "ไม่แนบรูป",
                "text": "ไม่แนบรูป",
            },
        },
    ]


def category_quick_reply() -> list[dict]:
    labels = ["ควันบุหรี่", "ไฟทางดับ", "กีดขวาง", "อื่นๆ"]
    return [
        {
            "type": "action",
            "action": {
                "type": "message",
                "label": lbl,
                "text": lbl,
            },
        }
        for lbl in labels
    ]


def upload_line_image_to_r2(message_id: str) -> str:
    if not s3_r2 or not R2_PUBLIC_BASE_URL:
        raise RuntimeError("ยังไม่ได้ตั้งค่า R2 ให้ครบถ้วน")

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


def update_hazard_fields(hazard_id: str, fields: dict) -> None:
    if not supabase or not hazard_id:
        return
    res = supabase.table("hazards").update(fields).eq("id", hazard_id).execute()
    if getattr(res, "error", None):
        raise RuntimeError(str(res.error))


def ask_first_photo_prompt(reply_token: str) -> None:
    send_line_message(
        reply_token,
        "คุณสามารถแจ้งเหตุที่พบ ใน มทรกรุงเทพได้เลยครับ สามารถส่งรูปเข้ามาได้เลยครับ",
        quick_items=photo_choice_quick_reply(),
    )


def ask_next_step_prompt(reply_token: str, prefix: str = "") -> None:
    body = (
        f"{prefix}\n" if prefix else ""
    ) + "กรุณาส่งตำแหน่งที่ตั้ง (Location) และเลือกเรื่องที่จะแจ้งครับ (หรือพิมพ์รายละเอียดเพิ่มเติมได้เลย)"
    send_line_message(reply_token, body, quick_items=category_quick_reply())


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
        sessions[user_id] = {
            "step": "intro_photo",
            "category": "other",
            "description": "",
            "lat": None,
            "lng": None,
            "image_url": None,
            "created_id": None,
        }
        sess = sessions[user_id]
        ask_first_photo_prompt(reply_token)
        return

    # 1) ผู้ใช้ส่งรูปภาพ
    if msg_type == "image":
        try:
            image_url = upload_line_image_to_r2(msg.get("id", ""))
            sess["image_url"] = image_url
            if sess.get("created_id"):
                update_hazard_fields(sess["created_id"], {"image_url": image_url})
            sess["step"] = "waiting_details"
            ask_next_step_prompt(reply_token, "รับรูปภาพเรียบร้อยแล้วครับ")
        except Exception as exc:
            print("[R2 upload error]:", exc)
            sess["step"] = "waiting_details"
            ask_next_step_prompt(
                reply_token,
                "อัปโหลดรูปไม่สำเร็จ แต่ดำเนินการแจ้งเหตุต่อได้ตามปกติครับ",
            )
        return

    # 2) ผู้ใช้ส่งตำแหน่งที่ตั้ง (Location) -> บันทึกลงหมุดและแจ้งรหัส
    if msg_type == "location":
        sess["lat"] = msg.get("latitude", DEFAULT_LAT)
        sess["lng"] = msg.get("longitude", DEFAULT_LNG)
        addr = (msg.get("address") or "").strip()
        if addr and not sess.get("description"):
            sess["description"] = addr

        try:
            if not sess.get("created_id"):
                hazard_id = insert_hazard(sess)
                sess["created_id"] = hazard_id
            else:
                hazard_id = sess["created_id"]
                update_hazard_fields(
                    hazard_id,
                    {
                        "lat": float(sess["lat"]),
                        "lng": float(sess["lng"]),
                        "description": sess.get("description") or CATEGORY_THAI_LABEL.get(sess["category"], "แจ้งจุดเสี่ยง"),
                        "image_url": sess.get("image_url"),
                    },
                )
            cat_label = CATEGORY_THAI_LABEL.get(sess["category"], "อื่นๆ")
            send_line_message(
                reply_token,
                f"ปักหมุดจุดเสี่ยงใหม่ รหัส {hazard_id} ลงแผนที่เรียบร้อยแล้วครับ ✅\nหมวดหมู่: {cat_label}\nสามารถเลือกหมวดหมู่หรือพิมพ์รายละเอียดเพิ่มเติมได้เลยครับ",
                quick_items=category_quick_reply(),
            )
        except Exception as exc:
            print("[Supabase save error]:", exc)
            send_line_message(reply_token, f"บันทึกลงระบบไม่สำเร็จ: {exc}")
        return

    # 3) ผู้ใช้ส่งข้อความ
    if msg_type == "text":
        text = (msg.get("text") or "").strip()
        lower = text.lower()

        if sess.get("step") == "intro_photo":
            if text in ("ไม่แนบรูป", "ข้าม", "ไม่ส่งรูป"):
                sess["image_url"] = None
                sess["step"] = "waiting_details"
                ask_next_step_prompt(reply_token, "รับทราบครับ ไม่แนบรูปภาพ")
                return
            if lower in CATEGORY_MAP:
                sess["category"] = CATEGORY_MAP[lower]
                sess["step"] = "waiting_details"
                ask_next_step_prompt(reply_token, f"เลือกเรื่อง \"{text}\" แล้วครับ")
                return
            if text in ("เริ่มแจ้ง", "แจ้งเหตุ", "สวัสดี", "เมนู"):
                ask_first_photo_prompt(reply_token)
                return

            ask_first_photo_prompt(reply_token)
            return

        if lower in CATEGORY_MAP:
            sess["category"] = CATEGORY_MAP[lower]
            if sess.get("created_id"):
                try:
                    update_hazard_fields(sess["created_id"], {"category": sess["category"]})
                except Exception as exc:
                    print("[Supabase category update error]:", exc)
            send_line_message(
                reply_token,
                f"บันทึกเรื่อง \"{CATEGORY_THAI_LABEL[sess['category']]}\" แล้วครับ\nหากยังไม่ได้ส่งตำแหน่ง สามารถส่ง Location มาได้เลยครับ",
                quick_items=category_quick_reply(),
            )
            return

        if text:
            sess["description"] = text
            if sess.get("created_id"):
                try:
                    update_hazard_fields(sess["created_id"], {"description": text})
                except Exception as exc:
                    print("[Supabase description update error]:", exc)
            send_line_message(
                reply_token,
                "บันทึกรายละเอียดเพิ่มเติมเรียบร้อยแล้วครับ ✅",
                quick_items=category_quick_reply(),
            )


@app.get("/")
def home():
    return "RMUTK SafeWalk Bot is running", 200


@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.post("/webhook")
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    raw_body = request.get_data()

    if not verify_line_signature(raw_body, signature):
        return jsonify({"error": "Invalid signature"}), 401

    payload = request.get_json(silent=True) or {}
    for event in payload.get("events", []):
        process_event(event)

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
