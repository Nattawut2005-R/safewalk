import base64
import hashlib
import hmac
import math
import os
import time
import uuid
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request
from supabase import create_client

app = Flask(__name__)

# ค่า Environment Variables (นำไปตั้งค่าในหน้า Render -> Environment)
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://cysxdienifyupfunevfs.supabase.co")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY) if SUPABASE_SERVICE_KEY else None

# เก็บสถานะชั่วคราวของผู้ใช้แต่ละคนในแชต LINE
user_sessions = {}

CATEGORY_LABELS = {
    "smoke": "สูบบุหรี่ในพื้นที่ห้ามสูบ",
    "dark": "ไฟทางดับ / ทางเดินมืด",
    "block": "ขายของปิดถนน / ตั้งเต็นท์",
    "other": "อื่นๆ",
}


def verify_line_signature(body_bytes: bytes, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET or not signature:
        return False
    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body_bytes,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def distance_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lng / 2) ** 2
    )
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def reply_line_message(reply_token: str, messages: list):
    if not LINE_CHANNEL_ACCESS_TOKEN or not reply_token:
        return
    try:
        requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            },
            json={"replyToken": reply_token, "messages": messages},
            timeout=10,
        )
    except Exception as exc:
        print("LINE reply error:", exc)


def category_quick_reply_message(text: str) -> dict:
    return {
        "type": "text",
        "text": text,
        "quickReply": {
            "items": [
                {
                    "type": "action",
                    "action": {"type": "message", "label": "🚬 ควันบุหรี่", "text": "แจ้ง:ควันบุหรี่"},
                },
                {
                    "type": "action",
                    "action": {"type": "message", "label": "💡 ไฟทางดับ", "text": "แจ้ง:ไฟทางดับ"},
                },
                {
                    "type": "action",
                    "action": {"type": "message", "label": "🚧 กีดขวาง", "text": "แจ้ง:กีดขวาง"},
                },
                {
                    "type": "action",
                    "action": {"type": "message", "label": "⚠️ อื่นๆ", "text": "แจ้ง:อื่นๆ"},
                },
                {
                    "type": "action",
                    "action": {"type": "location", "label": "📍 ส่งตำแหน่ง GPS"},
                },
            ]
        },
    }


def location_quick_reply_message(text: str) -> dict:
    return {
        "type": "text",
        "text": text,
        "quickReply": {
            "items": [
                {
                    "type": "action",
                    "action": {"type": "location", "label": "📍 กดส่งตำแหน่ง Location"},
                },
                {
                    "type": "action",
                    "action": {"type": "message", "label": "🔄 เปลี่ยนหมวดหมู่", "text": "เริ่มแจ้งเหตุ"},
                },
            ]
        },
    }


def save_hazard_to_supabase(
    category: str,
    lat: float,
    lng: float,
    description: str,
    image_url: str = "",
):
    if not supabase:
        raise RuntimeError("Supabase client ยังไม่ได้ตั้งค่า SUPABASE_SERVICE_KEY")

    now_iso = datetime.now(timezone.utc).isoformat()
    existing_rows = (
        supabase.table("hazards")
        .select("*")
        .eq("category", category)
        .eq("is_resolved", False)
        .execute()
        .data
        or []
    )

    near_item = None
    for row in existing_rows:
        dist = distance_meters(float(row["lat"]), float(row["lng"]), lat, lng)
        if dist <= 25.0:
            near_item = row
            break

    if near_item:
        next_count = int(near_item.get("report_count") or 1) + 1
        old_desc = (near_item.get("description") or "").strip()
        new_desc = f"{old_desc} • {description}".strip(" •") if description else old_desc

        update_payload = {
            "report_count": next_count,
            "description": new_desc,
            "created_at": now_iso,
        }
        if image_url and not near_item.get("image_url"):
            update_payload["image_url"] = image_url

        supabase.table("hazards").update(update_payload).eq("id", near_item["id"]).execute()
        return near_item["id"], True

    new_id = f"HZ-LINE-{int(time.time() * 1000) % 1000000:06d}-{uuid.uuid4().hex[:3].upper()}"
    supabase.table("hazards").insert(
        {
            "id": new_id,
            "category": category,
            "lat": lat,
            "lng": lng,
            "description": description or f"แจ้งผ่าน LINE: {CATEGORY_LABELS.get(category, category)}",
            "image_url": image_url,
            "report_count": 1,
            "not_fixed_count": 0,
            "is_iot_alert": False,
            "is_resolved": False,
            "resolved_at": None,
            "created_at": now_iso,
        }
    ).execute()
    return new_id, False


@app.route("/", methods=["GET"])
def index():
    return jsonify({"service": "RMUTK SafeWalk LINE Bot", "status": "running"}), 200


@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@app.route("/webhook/line", methods=["POST"])
def line_webhook():
    signature = request.headers.get("X-Line-Signature", "")
    raw_body = request.get_data()

    if not verify_line_signature(raw_body, signature):
        return "Invalid signature", 403

    payload = request.get_json(silent=True) or {}
    events = payload.get("events", [])

    for event in events:
        event_type = event.get("type")
        reply_token = event.get("replyToken")
        user_id = event.get("source", {}).get("userId", "anonymous")

        session = user_sessions.setdefault(
            user_id,
            {"category": "smoke", "pending_note": "", "last_hazard_id": None},
        )

        # กรณีแอดเพื่อนใหม่
        if event_type == "follow":
            reply_line_message(
                reply_token,
                [
                    category_quick_reply_message(
                        "ยินดีต้อนรับสู่ RMUTK SafeWalk ระบบแจ้งเตือนจุดเสี่ยง มทร.กรุงเทพ\nเลือกหมวดหมู่ที่ต้องการแจ้งด้านล่างได้เลยครับ 👇"
                    )
                ],
            )
            continue

        if event_type != "message":
            continue

        msg = event.get("message", {})
        msg_type = msg.get("type")

        if msg_type == "text":
            text = (msg.get("text") or "").strip()

            if text in ("เริ่มแจ้งเหตุ", "แจ้งเหตุ", "สวัสดี", "เมนู"):
                session["last_hazard_id"] = None
                session["pending_note"] = ""
                reply_line_message(
                    reply_token,
                    [category_quick_reply_message("เลือกหมวดหมู่จุดเสี่ยงที่พบใน มทร.กรุงเทพ ได้เลยครับ 👇")],
                )

            elif text == "แจ้ง:ควันบุหรี่":
                session["category"] = "smoke"
                session["last_hazard_id"] = None
                reply_line_message(
                    reply_token,
                    [location_quick_reply_message("เลือกหมวด [🚬 ควันบุหรี่] แล้ว\nกดปุ่ม 📍 ด้านล่างเพื่อส่งตำแหน่ง Location ได้เลยครับ (หรือพิมพ์จุดสังเกตก่อนส่งตำแหน่งก็ได้)")],
                )

            elif text == "แจ้ง:ไฟทางดับ":
                session["category"] = "dark"
                session["last_hazard_id"] = None
                reply_line_message(
                    reply_token,
                    [location_quick_reply_message("เลือกหมวด [💡 ไฟทางดับ] แล้ว\nกดปุ่ม 📍 ด้านล่างเพื่อส่งตำแหน่ง Location ได้เลยครับ")],
                )

            elif text == "แจ้ง:กีดขวาง":
                session["category"] = "block"
                session["last_hazard_id"] = None
                reply_line_message(
                    reply_token,
                    [location_quick_reply_message("เลือกหมวด [🚧 กีดขวางทางเดิน] แล้ว\nกดปุ่ม 📍 ด้านล่างเพื่อส่งตำแหน่ง Location ได้เลยครับ")],
                )

            elif text == "แจ้ง:อื่นๆ":
                session["category"] = "other"
                session["last_hazard_id"] = None
                reply_line_message(
                    reply_token,
                    [location_quick_reply_message("เลือกหมวด [⚠️ อื่นๆ] แล้ว\nกดปุ่ม 📍 ด้านล่างเพื่อส่งตำแหน่ง Location ได้เลยครับ")],
                )

            else:
                # ถ้าเพิ่งบันทึกหมุดเสร็จแล้วพิมพ์ข้อความตามหลัง -> อัปเดตรายละเอียดเพิ่มให้จุดล่าสุดทันที
                if session.get("last_hazard_id") and supabase:
                    target_id = session["last_hazard_id"]
                    existing = supabase.table("hazards").select("description").eq("id", target_id).execute().data
                    if existing:
                        old_desc = (existing[0].get("description") or "").strip()
                        updated_desc = f"{old_desc} ({text})" if old_desc else text
                        supabase.table("hazards").update({"description": updated_desc}).eq("id", target_id).execute()

                    reply_line_message(
                        reply_token,
                        [
                            category_quick_reply_message(
                                f"บันทึกรายละเอียดเพิ่มเติมให้รหัส {target_id} เรียบร้อยแล้วครับ ✅\nหากต้องการแจ้งจุดใหม่ เลือกหมวดด้านล่างได้เลย"
                            )
                        ],
                    )
                else:
                    # ถ้าพิมพ์รายละเอียดก่อนส่ง Location -> เก็บข้อความรอไว้รวมตอนส่งพิกัด
                    session["pending_note"] = text
                    reply_line_message(
                        reply_token,
                        [
                            location_quick_reply_message(
                                f"รับทราบรายละเอียด: \"{text}\"\nต่อไปกดปุ่ม 📍 เพื่อส่งตำแหน่ง Location ใน มทร.กรุงเทพ ได้เลยครับ"
                            )
                        ],
                    )

        elif msg_type == "location":
            lat = float(msg.get("latitude"))
            lng = float(msg.get("longitude"))
            address = (msg.get("address") or "").strip()
            cat = session.get("category", "smoke")
            note = (session.get("pending_note") or "").strip()

            full_desc = " • ".join([part for part in [note, address] if part])
            hazard_id, merged = save_hazard_to_supabase(cat, lat, lng, full_desc)

            session["last_hazard_id"] = hazard_id
            session["pending_note"] = ""

            status_txt = (
                f"รวมเข้ากับจุดใกล้เคียง (≤ 25 ม.) รหัส {hazard_id} เรียบร้อยแล้วครับ 🔄"
                if merged
                else f"ปักหมุดจุดเสี่ยงใหม่ รหัส {hazard_id} ลงแผนที่เรียบร้อยแล้วครับ ✅"
            )

            reply_line_message(
                reply_token,
                [
                    category_quick_reply_message(
                        f"{status_txt}\n"
                        f"หมวดหมู่: {CATEGORY_LABELS.get(cat, cat)}\n"
                        f"พิมพ์รายละเอียดเพิ่มเติมต่อได้เลย หรือดูแผนที่ได้ที่:\n"
                        f"https://nattawut2005-r.github.io/safewalk/"
                    )
                ],
            )

        elif msg_type == "image":
            # จุดเชื่อมต่อสำหรับขั้นตอนถัดไป (ดึงรูปจาก LINE Content API ส่งขึ้น Cloudflare R2)
            reply_line_message(
                reply_token,
                [
                    location_quick_reply_message(
                        "ได้รับรูปภาพแล้วครับ 📸 (ระบบอัปโหลดรูป R2 กำลังเชื่อมต่อ)\nกรุณากดปุ่ม 📍 ส่งตำแหน่ง Location เพื่อบันทึกจุดลงแผนที่ได้เลยครับ"
                    )
                ],
            )

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
