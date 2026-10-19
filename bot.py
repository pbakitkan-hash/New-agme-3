import os
import hmac
import hashlib
import json
import time
import threading
from urllib.parse import parse_qsl, quote
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

TOKEN = os.environ.get('BOT_TOKEN')
if not TOKEN:
    raise ValueError("ОШИБКА: Переменная BOT_TOKEN не найдена на Render!")

WEB_APP_URL = os.environ.get('WEB_APP_URL', 'https://pbakitkan-hash.github.io/New-agme-3/')
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TOKEN}"

# --- ссылки, общая база игроков (та же, что в игре), промокоды, звёзды
NPOINT_URL = os.environ.get('NPOINT_URL', 'https://api.npoint.io/2816c8000051e7c162ec')
NEWS_URL = 'https://t.me/Brainrotscase'
SUPPORT_URL = 'https://t.me/parasras'
PROMO_PROMPT = '🎁 Отправьте бонус-код ответом на это сообщение'
STAR_RATE = 1.5 # 1 звезда = 1.5 монеты
NEWS_CHAT = '@Brainrotscase' # канал для проверки подписки (бот должен быть админом канала)
BOT_USERNAME = os.environ.get('BOT_USERNAME', 'Brainrotscase_bot')
SHARE_IMAGE = os.environ.get('SHARE_IMAGE', WEB_APP_URL.rstrip('/') + '/banner.jpg') # картинка в сообщении для друзей (файл banner.jpg лежит рядом с index.html)
SHARE_THUMB = os.environ.get('SHARE_THUMB', WEB_APP_URL.rstrip('/') + '/banner_s.jpg')
SHARE_TEXT = "🎁 Получай бесплатный кейс в нашем боте.\n\n» Апгрейди брейнротов\n» Получай бонусы\n\n🔗 Заходи в Brainrot Case"
SHARE_COOLDOWN_MS = 12 * 3600 * 1000
BOT_NICK = 'brainrotscase_bot' # что должно быть в имени игрока для «кейса за ник»

app = Flask(__name__)
CORS(app)

http = requests.Session() # одно соединение на все запросы быстрее
cloud_lock = threading.Lock() # записи в общую базу идут по очереди, чтобы не затирать друг друга
known_ids = set() # кого бот уже запомнил (не дёргаем базу лишний раз)
users_db = {}

def game_url():
    """Ссылка на игру: бот сам передаёт игре свой адрес (?api=...), v= сбрасывает старый кэш."""
    api = os.environ.get('RENDER_EXTERNAL_URL', '')
    if not api:
        return WEB_APP_URL
    sep = '&' if '?' in WEB_APP_URL else '?'
    return f"{WEB_APP_URL}{sep}api={quote(api, safe='')}&v=5"

@app.route('/')
def home():
    return "BRAINROT CASE Backend is running!"

@app.route('/ping')
def ping():
    # лёгкий адрес для cron-job: отвечает мгновенно и не трогает базу
    return "pong"

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

# --- API для игры: счёт на звёзды и проверка ника/подписки
def verify_init_data(init_data):
    """Проверка подписи Telegram: возвращает данные пользователя или None."""
    try:
        pairs = dict(parse_qsl(init_data or "", keep_blank_values=True))
        got = pairs.pop("hash", None)
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if got and hmac.compare_digest(calc, got):
            return json.loads(pairs["user"])
    except Exception as e:
        print("initData error:", e)
    return None

@app.route('/api/invoice', methods=['POST'])
def api_invoice():
    data = request.get_json(silent=True) or {}
    if not verify_init_data(data.get("initData")):
        return jsonify({"error": "unauthorized"}), 403
    try:
        stars = int(data.get("stars", 0))
    except Exception:
        stars = 0
    if not 1 <= stars <= 10000:
        return jsonify({"error": "bad stars"}), 400
    
    coins = round(stars * STAR_RATE, 2)
    r = http.post(f"{TELEGRAM_API_URL}/createInvoiceLink", json={
        "title": "Пополнение баланса",
        "description": f"{stars} ⭐️ = {coins} BC",
        "payload": f"topup_{stars}",
        "currency": "XTR",
        "prices": [{"label": "BC", "amount": stars}],
    }, timeout=10).json()
    
    if not r.get("ok"):
        return jsonify({"error": r.get("description", "telegram error")}), 502
    return jsonify({"link": r["result"]})

@app.route('/api/check', methods=['POST'])
def api_check():
    data = request.get_json(silent=True) or {}
    user = verify_init_data(data.get("initData"))
    if not user:
        return jsonify({"error": "unauthorized"}), 403
    
    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".lower()
    sub = None # None = проверить не удалось (бот не админ канала)
    try:
        m = http.get(f"{TELEGRAM_API_URL}/getChatMember", params={"chat_id": NEWS_CHAT, "user_id": user["id"]}, timeout=10).json()
        if m.get("ok"):
            sub = m["result"]["status"] in ("creator", "administrator", "member", "restricted")
    except Exception as e:
        print("getChatMember error:", e)
        
    touch_user(user) # заодно запоминаем игрока
    return jsonify({"nick": BOT_NICK in name, "sub": sub})

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    update = request.get_json(silent=True) or {}
    if "pre_checkout_query" in update: # оплата звёздами: подтверждаем счёт сразу
        tg("answerPreCheckoutQuery", pre_checkout_query_id=update["pre_checkout_query"]["id"], ok=True)
    else:
        # всё остальное в фоне: Telegram получает ответ мгновенно, бот не «тормозит»
        threading.Thread(target=process_update, args=(update,), daemon=True).start()
    return "OK", 200

def process_update(update):
    try:
        if "message" in update:
            on_message(update["message"])
            touch_user(update["message"].get("from", {}))
        elif "callback_query" in update:
            on_callback(update["callback_query"])
            touch_user(update["callback_query"].get("from", {}))
        elif "inline_query" in update:
            on_inline_query(update["inline_query"])
        elif "chosen_inline_result" in update:
            on_chosen(update["chosen_inline_result"])
    except Exception as e:
        print("handler error:", e)

def tg(method, **payload):
    try:
        http.post(f"{TELEGRAM_API_URL}/{method}", json=payload, timeout=10)
    except Exception as e:
        print("telegram error:", e)

def send_welcome(chat_id):
    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "🎮 Играть в BRAINROT CASE",
                    "web_app": {"url": game_url()}
                }
            ],
            [
                {
                    "text": "🎁 Забрать бесплатный кейс",
                    "style": "success",
                    "switch_inline_query_chosen_chat": {
                        "query": "share",
                        "allow_user_chats": True,
                        "allow_group_chats": True,
                        "allow_bot_chats": False,
                        "allow_channel_chats": False
                    }
                }
            ],
            [
                {"text": "📢 Новости", "url": NEWS_URL},
                {"text": "💬 Поддержка", "url": SUPPORT_URL}
            ],
            [
                {"text": "🔑 Ввести бонус-код", "callback_data": "promo"}
            ]
        ]
    }
    tg("sendMessage", chat_id=chat_id, 
       text="Привет! 👋\n\nДобро пожаловать в BRAINROT CASE!\nНажми кнопку ниже, чтобы открыть приложение, крутить кейсы и фармить BC!", 
       reply_markup=keyboard)

# --- Сообщения и кнопки
def ask_promo(chat_id):
    # force_reply: ответ пользователя приходит как reply на это сообщение (работает и после перезапуска сервера)
    tg("sendMessage", chat_id=chat_id, text=PROMO_PROMPT, 
       reply_markup={"force_reply": True, "input_field_placeholder": "Бонус-код"})

def on_payment(msg):
    pay = msg["successful_payment"]
    if pay.get("currency") != "XTR":
        return
    user, stars, charge = msg["from"], int(pay["total_amount"]), pay.get("telegram_payment_charge_id")
    
    with cloud_lock:
        users = load_users()
        rec = find(users, user["id"])
        if rec is None:
            rec = {"id": user["id"], "username": name_of(user), "score": 0}
            users.append(rec)
        paid = rec.setdefault("paid", [])
        if charge in paid:
            return # платёж уже зачислен
        coins = round(stars * STAR_RATE, 2)
        rec["grant"] = round(rec.get("grant", 0) + coins, 2)
        rec["paid"] = (paid + [charge])[-50:]
        save_users(users)
        
    tg("sendMessage", chat_id=msg["chat"]["id"], text=f"✅ Пополнение: {stars} ⭐️ (+{coins} BC). Монеты уже в игре!")

def on_message(msg):
    if msg.get("successful_payment"):
        try:
            on_payment(msg)
        except Exception as e:
            print("payment error:", e)
        return

    chat_id = msg["chat"]["id"]
    user = msg.get("from", {})
    text = (msg.get("text") or "").strip()
    cmd, _, rest = text.partition(" ")
    cmd = cmd.split("@")[0]
    rest = rest.strip()

    if cmd == "/start":
        send_welcome(chat_id)
        if rest.startswith("ref_"):
            try:
                register_referral(user, rest) # сначала отвечаем, чтобы игрок не ждёт базу
            except Exception as e:
                print("referral error:", e)
    elif cmd == "/promo":
        try_redeem(chat_id, user, rest) if rest else ask_promo(chat_id)
    elif text and not text.startswith("/") and PROMO_PROMPT in ((msg.get("reply_to_message") or {}).get("text") or ""):
        try_redeem(chat_id, user, text)

def on_inline_query(q):
    """Игрок выбрал чат: показываем сообщение для друзей (картинка + текст + кнопка входа с его реф-ссылкой)."""
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{q['from']['id']}"
    result = {
        "type": "photo",
        "id": "share",
        "photo_url": SHARE_IMAGE,
        "thumbnail_url": SHARE_THUMB,
        "photo_width": 1280,
        "photo_height": 714,
        "title": "🎁 Бесплатный кейс",
        "description": "Отправь друзьям и забери кейс",
        "caption": SHARE_TEXT,
        "reply_markup": {"inline_keyboard": [[{"text": "🔗 Заходи в Brainrot Case", "url": link}]]}
    }
    tg("answerInlineQuery", inline_query_id=q["id"], results=[result], cache_time=0, is_personal=True)

def on_chosen(c):
    """Telegram присылает это, когда игрок реально отправил сообщение в чат — тогда открываем бесплатный кейс."""
    if c.get("result_id") != "share":
        return
    user, now = c["from"], int(time.time() * 1000)
    granted = False
    
    with cloud_lock:
        users = load_users()
        rec = find(users, user["id"])
        if rec is None:
            rec = {"id": user["id"], "username": name_of(user), "score": 0, "first": now, "seen": now}
            users.append(rec)
        if now - rec.get("lastShare", 0) >= SHARE_COOLDOWN_MS: # одно разблокирование в 12 часов
            rec["shareAdd"] = rec.get("shareAdd", 0) + 1
            rec["lastShare"] = now
            granted = True
            save_users(users)
            
    known_ids.add(user["id"])
    tg("sendMessage", chat_id=user["id"], 
       text="✅ Сообщение отправлено! Бесплатный кейс ждёт тебя в игре (Профиль ➔ Бонусы)." if granted 
       else "⏳ Ты уже получал кейс за отправку. Следующий — через 12 часов с прошлой отправки.")

def on_callback(q):
    tg("answerCallbackQuery", callback_query_id=q["id"])
    if q.get("data") == "promo":
        ask_promo(q["message"]["chat"]["id"])

# --- Общая база игроков (npoint)
def load_users():
    r = http.get(NPOINT_URL, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []

def save_users(users):
    # npoint обновляет документ методом POST; если не принял — пробуем PUT
    last = None
    for send in (http.post, http.put):
        try:
            r = send(NPOINT_URL, json=users, timeout=10)
            if r.ok:
                return
            last = r.status_code
        except Exception as e:
            last = e
    raise RuntimeError(f"npoint: запись не удалась ({last})")

def find(users, uid):
    return next((u for u in users if u.get("id") == uid), None)

def name_of(user):
    return user.get("username") or user.get("first_name") or "Player"

def touch_user(user):
    """Запоминаем любого, кто зашёл: создаём запись в базе, если её ещё нет."""
    uid = user.get("id")
    if not uid or uid in known_ids:
        return
    try:
        with cloud_lock:
            users = load_users()
            if find(users, uid) is None:
                now = int(time.time() * 1000)
                users.append({"id": uid, "username": name_of(user), "score": 0, "first": now, "seen": now})
                save_users(users)
        known_ids.add(uid)
    except Exception as e:
        print("touch error:", e)

def register_referral(user, payload):
    """/start ref_<id>: засчитываем только совсем нового игрока и только один раз."""
    inviter_id = int(payload.split("_", 1)[1])
    uid = user["id"]
    if inviter_id == uid:
        return
        
    with cloud_lock:
        users = load_users()
        if find(users, uid) is not None:
            return
        inviter = find(users, inviter_id)
        if inviter is None:
            return
        now = int(time.time() * 1000)
        users.append({"id": uid, "username": name_of(user), "score": 0, "ref": inviter_id, "first": now, "seen": now})
        inviter["tkAdd"] = inviter.get("tkAdd", 0) + 1
        inviter["refCount"] = inviter.get("refCount", 0) + 1
        save_users(users)
        
    known_ids.add(uid)
    tg("sendMessage", chat_id=inviter_id, text=f"👤 По вашей ссылке пришёл @{name_of(user)}! Вам начислен билет на реферальный кейс.")

def redeem(user, code):
    with cloud_lock:
        users = load_users()
        uid = user["id"]
        codes = (find(users, "_promos_") or {}).get("codes", {})
        key = code if code in codes else (code.upper() if code.upper() in codes else None)
        if key is None:
            return "❌ Такого бонус-кода нет."
            
        info = codes[key]
        if info.get("owner") == uid:
            return "❌ Нельзя использовать собственный код."
        if uid in info.get("usedBy", []):
            return "❌ Вы уже использовали этот код."
            
        rec = find(users, uid)
        if rec is None:
            rec = {"id": uid, "username": name_of(user), "score": 0}
            users.append(rec)
            
        rec["grant"] = round(rec.get("grant", 0) + info["reward"], 2)
        info.setdefault("usedBy", []).append(uid)
        
        owner = find(users, info["owner"]) if info.get("owner") else None
        if owner: # личный промокод игрока: ему тоже награда + билет
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

def keep_alive():
    """Не даём бесплатному Render засыпать: раз в 10 минут стучимся в свой /ping (вместе с cron-job)."""
    url = os.environ.get('RENDER_EXTERNAL_URL')
    while url:
        time.sleep(600)
        try:
            http.get(f"{url}/ping", timeout=10)
        except Exception:
            pass

if __name__ == '__main__':
    render_url = os.environ.get('RENDER_EXTERNAL_URL')
    if render_url:
        webhook_url = f"{render_url}/{TOKEN}"
        http.post(f"{TELEGRAM_API_URL}/setWebhook", json={
            "url": webhook_url,
            "allowed_updates": ["message", "callback_query", "pre_checkout_query", "inline_query", "chosen_inline_result"],
        }, timeout=10)
        
    threading.Thread(target=keep_alive, daemon=True).start()
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port, threaded=True)
