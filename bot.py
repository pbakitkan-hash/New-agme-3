import os
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

TOKEN = os.environ.get('BOT_TOKEN')
if not TOKEN:
    raise ValueError("ОШИБКА: Переменная BOT_TOKEN не найдена на Render!")

WEB_APP_URL = os.environ.get('WEB_APP_URL', 'https://pbakitkan-hash.github.io/New-agme-3/')
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TOKEN}"

# --- новое: ссылки, общая база игроков (та же, что в игре), промокоды ---
NPOINT_URL = os.environ.get('NPOINT_URL', 'https://api.npoint.io/2816c8000051e7c162ec')
NEWS_URL = 'https://t.me/sipibu'
SUPPORT_URL = 'https://t.me/parasras'
PROMO_PROMPT = '🎁 Отправьте бонус-код ответом на это сообщение 👇'

app = Flask(__name__)
CORS(app)

users_db = {}


@app.route('/')
def home():
    return "BRAINROT CASE Backend is running!"


@app.route('/api/user/<int:user_id>', methods=['GET', 'POST'])
def handle_user(user_id):
    if request.method == 'GET':
        user_data = users_db.get(user_id, {"balance": 150.0, "inventory": []})
        return jsonify(user_data)
    elif request.method == 'POST':
        data = request.json
        if data:
            users_db[user_id] = data
            return jsonify({"status": "success", "saved": data})
        return jsonify({"status": "error", "message": "No data provided"}), 400


@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    update = request.get_json(silent=True) or {}
    try:
        if "message" in update:
            on_message(update["message"])
        elif "callback_query" in update:
            on_callback(update["callback_query"])
    except Exception as e:
        print("handler error:", e)
    return "OK", 200


def tg(method, **payload):
    try:
        requests.post(f"{TELEGRAM_API_URL}/{method}", json=payload, timeout=10)
    except Exception as e:
        print("telegram error:", e)


def send_welcome(chat_id):
    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "🎮 Играть в BRAINROT CASE",
                    "web_app": {"url": WEB_APP_URL}
                }
            ],
            [
                {"text": "📰 Новости", "url": NEWS_URL},
                {"text": "💬 Поддержка", "url": SUPPORT_URL}
            ],
            [
                {"text": "🎁 Ввести бонус-код", "callback_data": "promo"}
            ]
        ]
    }
    payload = {
        "chat_id": chat_id,
        "text": "Привет! 🔥\n\nДобро пожаловать в BRAINROT CASE!\nНажми кнопку ниже, чтобы открыть приложение, крутить кейсы и фармить BC!",
        "reply_markup": keyboard
    }
    requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)


# ---------- сообщения и кнопки ----------
def ask_promo(chat_id):
    # force_reply: ответ пользователя приходит как reply на это сообщение — работает и после перезапуска сервера
    tg("sendMessage", chat_id=chat_id, text=PROMO_PROMPT,
       reply_markup={"force_reply": True, "input_field_placeholder": "Бонус-код"})


def on_message(msg):
    chat_id = msg["chat"]["id"]
    user = msg.get("from", {})
    text = (msg.get("text") or "").strip()
    cmd, _, rest = text.partition(" ")
    cmd = cmd.split("@")[0]
    rest = rest.strip()
    if cmd == "/start":
        if rest.startswith("ref_"):
            try:
                register_referral(user, rest)
            except Exception as e:
                print("referral error:", e)
        send_welcome(chat_id)
    elif cmd == "/promo":
        try_redeem(chat_id, user, rest) if rest else ask_promo(chat_id)
    elif text and not text.startswith("/") and PROMO_PROMPT in ((msg.get("reply_to_message") or {}).get("text") or ""):
        try_redeem(chat_id, user, text)


def on_callback(q):
    tg("answerCallbackQuery", callback_query_id=q["id"])
    if q.get("data") == "promo":
        ask_promo(q["message"]["chat"]["id"])


# ---------- общая база игроков (npoint) ----------
def load_users():
    r = requests.get(NPOINT_URL, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def save_users(users):
    requests.put(NPOINT_URL, json=users, timeout=10).raise_for_status()


def find(users, uid):
    return next((u for u in users if u.get("id") == uid), None)


def name_of(user):
    return user.get("username") or user.get("first_name") or "Player"


def register_referral(user, payload):
    """/start ref_<id>: засчитываем только совсем нового игрока и только один раз."""
    inviter_id = int(payload.split("_", 1)[1])
    uid = user["id"]
    if inviter_id == uid:
        return
    users = load_users()
    if find(users, uid) is not None:
        return
    inviter = find(users, inviter_id)
    if inviter is None:
        return
    users.append({"id": uid, "username": name_of(user), "score": 0, "ref": inviter_id})
    inviter["tkAdd"] = inviter.get("tkAdd", 0) + 1
    inviter["refCount"] = inviter.get("refCount", 0) + 1
    save_users(users)
    tg("sendMessage", chat_id=inviter_id,
       text=f"🎉 По вашей ссылке пришёл @{name_of(user)}! Вам начислен 🎟️ билет на реферальный кейс.")


def redeem(user, code):
    users = load_users()
    uid = user["id"]
    codes = (find(users, "__promos__") or {}).get("codes", {})
    key = code if code in codes else (code.upper() if code.upper() in codes else None)
    if key is None:
        return "❌ Такого бонус-кода нет."
    info = codes[key]
    if info.get("owner") == uid:
        return "⚠️ Нельзя использовать собственный код."
    if uid in info.get("usedBy", []):
        return "⚠️ Вы уже использовали этот код."
    rec = find(users, uid)
    if rec is None:
        rec = {"id": uid, "username": name_of(user), "score": 0}
        users.append(rec)
    rec["grant"] = round(rec.get("grant", 0) + info["reward"], 2)
    info.setdefault("usedBy", []).append(uid)
    owner = find(users, info["owner"]) if info.get("owner") else None
    if owner:  # личный промокод игрока: ему тоже награда + билет
        owner["grant"] = round(owner.get("grant", 0) + info.get("ownerReward", 0), 2)
        owner["tkAdd"] = owner.get("tkAdd", 0) + 1
        owner["refCount"] = owner.get("refCount", 0) + 1
    save_users(users)
    return f"✅ Бонус-код принят! +{info['reward']} BC. Баланс обновится в игре в течение минуты."


def try_redeem(chat_id, user, code):
    try:
        text = redeem(user, code)
    except Exception as e:
        print("redeem error:", e)
        text = "⚠️ Не получилось связаться с базой, попробуйте позже."
    tg("sendMessage", chat_id=chat_id, text=text)


if __name__ == '__main__':
    render_url = os.environ.get('RENDER_EXTERNAL_URL')
    if render_url:
        webhook_url = f"{render_url}/{TOKEN}"
        requests.get(f"{TELEGRAM_API_URL}/setWebhook?url={webhook_url}")

    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
