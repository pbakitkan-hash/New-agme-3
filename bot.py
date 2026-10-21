import os
import hmac
import hashlib
import json
import time
import random
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

# --- ссылки, общая база игроков (та же, что в игре), промокоды, звёзды ---
NPOINT_URL = os.environ.get('NPOINT_URL', 'https://api.npoint.io/2816c8000051e7c162ec')
NEWS_URL = 'https://t.me/Brainrotscase'
SUPPORT_URL = 'https://t.me/parasras'
PROMO_PROMPT = '🎁 Отправьте бонус-код ответом на это сообщение 👇'
STAR_RATE = 1.5                  # 1 звезда = 1.5 монеты
NEWS_CHAT = '@Brainrotscase'     # канал для проверки подписки (бот должен быть админом канала)
BOT_USERNAME = os.environ.get('BOT_USERNAME', 'Brainrotscase_bot')
SHARE_IMAGE = os.environ.get('SHARE_IMAGE', WEB_APP_URL.rstrip('/') + '/banner.jpg')       # картинка в сообщении для друзей
SHARE_THUMB = os.environ.get('SHARE_THUMB', WEB_APP_URL.rstrip('/') + '/banner_s.jpg')
SHARE_TEXT = "🎁 Получай бесплатный кейс в нашем боте\n\n» Апгрейди брейнрота\n» Забирай бонус\n\n🏆 Заходи в Brainrot Case"
SHARE_COOLDOWN_MS = 12 * 3600 * 1000
BOT_NICK = 'brainrotscase_bot'   # что должно быть в имени игрока для «кейса за ник»

app = Flask(__name__)
CORS(app)

http = requests.Session()        # одно соединение на все запросы — быстрее
cloud_lock = threading.Lock()    # записи бота в общую базу идут по очереди
cloud_len = [0]                  # сколько записей видели в базе (защита от затирания при сбое чтения)
known_ids = set()                # кого бот уже запомнил

users_db = {}


def now_ms():
    return int(time.time() * 1000)


def game_url():
    """Ссылка на игру: бот сам передаёт игре свой адрес (?api=...), v= сбрасывает старый кэш."""
    api = os.environ.get('RENDER_EXTERNAL_URL', '')
    if not api:
        return WEB_APP_URL
    sep = '&' if '?' in WEB_APP_URL else '?'
    return f"{WEB_APP_URL}{sep}api={quote(api, safe='')}&v=6"


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


# ---------- общая база игроков (npoint): чтение → изменение → запись → проверка → повтор при конфликте ----------
def load_users():
    r = http.get(NPOINT_URL, timeout=10)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError("npoint вернул не список")
    return data


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


def canon(o):
    if isinstance(o, float) and o.is_integer():
        return int(o)
    if isinstance(o, dict):
        return {k: canon(v) for k, v in o.items()}
    if isinstance(o, list):
        return [canon(v) for v in o]
    return o


def same(a, b):
    return json.dumps(canon(a), sort_keys=True) == json.dumps(canon(b), sort_keys=True)


def cloud_update(mutator, tries=4):
    """mutator(users) меняет список и возвращает {"ids": [...]} или {"skip": True}.
    После записи проверяем, что наши записи на месте; если кто-то писал одновременно — повторяем на свежих данных."""
    last = None
    with cloud_lock:
        for _ in range(tries):
            try:
                users = load_users()
                if len(users) < cloud_len[0]:
                    raise RuntimeError("неполное чтение базы")
                out = mutator(users)
                if not out or out.get("skip"):
                    return out
                save_users(users)
                back = load_users()
                if all(same(find(back, i), find(users, i)) for i in out.get("ids", [])):
                    cloud_len[0] = max(cloud_len[0], len(back))
                    return out
            except Exception as e:
                last = e
            time.sleep(0.25 + random.random() * 0.7)
    raise last or RuntimeError("конфликт записи")


def name_of(user):
    return user.get("username") or user.get("first_name") or "Player"


def new_rec(user):
    t = now_ms()
    return {"id": user["id"], "username": name_of(user), "score": 0, "first": t, "seen": t}


def touch_user(user):
    """Запоминаем любого, кто зашёл: создаём запись в базе, если её ещё нет."""
    uid = user.get("id")
    if not uid or uid in known_ids:
        return

    def mut(users):
        if find(users, uid) is not None:
            return {"skip": True}
        users.append(new_rec(user))
        return {"ids": [uid]}
    try:
        cloud_update(mut)
        known_ids.add(uid)
    except Exception as e:
        print("touch error:", e)


def register_referral(user, payload):
    """/start ref_<id>: засчитываем только совсем нового игрока и только один раз."""
    inviter_id = int(payload.split("_", 1)[1])
    uid = user["id"]
    if inviter_id == uid:
        return
    st = {"applied": False}

    def mut(users):
        if find(users, uid) is not None:
            return {"skip": True}
        inviter = find(users, inviter_id)
        if inviter is None:
            return {"skip": True}
        rec = new_rec(user)
        rec["ref"] = inviter_id
        users.append(rec)
        inviter["tkT"] = inviter.get("tkT", 0) + 1
        inviter["refCount"] = inviter.get("refCount", 0) + 1
        st["applied"] = True
        return {"ids": [uid, inviter_id]}
    cloud_update(mut)
    known_ids.add(uid)
    if st["applied"]:
        tg("sendMessage", chat_id=inviter_id,
           text=f"🎉 По вашей ссылке пришёл @{name_of(user)}! Вам начислен 🎟️ билет на реферальный кейс.")


def redeem(user, code):
    uid = user["id"]
    st = {"applied": False, "msg": None, "reward": 0}

    def mut(users):
        codes = (find(users, "__promos__") or {}).get("codes", {})
        key = code if code in codes else (code.upper() if code.upper() in codes else None)
        if key is None:
            st["msg"] = "❌ Такого бонус-кода нет."
            return {"skip": True}
        info = codes[key]
        if info.get("owner") == uid:
            st["msg"] = "⚠️ Нельзя использовать собственный код."
            return {"skip": True}
        if uid in info.get("usedBy", []):
            if not st["applied"]:
                st["msg"] = "⚠️ Вы уже использовали этот код."
            return {"skip": True}
        rec = find(users, uid)
        if rec is None:
            rec = new_rec(user)
            users.append(rec)
        rec["grantT"] = round(rec.get("grantT", 0) + info["reward"], 2)   # накопительный счётчик: игра сама заберёт разницу
        info.setdefault("usedBy", []).append(uid)
        ids = ["__promos__", uid]
        owner = find(users, info["owner"]) if info.get("owner") else None
        if owner:  # личный промокод игрока: ему тоже награда + билет
            owner["grantT"] = round(owner.get("grantT", 0) + info.get("ownerReward", 0), 2)
            owner["tkT"] = owner.get("tkT", 0) + 1
            owner["refCount"] = owner.get("refCount", 0) + 1
            ids.append(owner["id"])
        st.update(applied=True, msg=None, reward=info["reward"])
        return {"ids": ids}
    cloud_update(mut)
    return st["msg"] or f"✅ Бонус-код принят! +{st['reward']} BC. Баланс обновится в игре в течение минуты."


def try_redeem(chat_id, user, code):
    try:
        text = redeem(user, code)
    except Exception as e:
        print("redeem error:", e)
        text = "⚠️ Не получилось связаться с базой, попробуйте через минуту."
    tg("sendMessage", chat_id=chat_id, text=text)


def on_payment(msg):
    pay = msg["successful_payment"]
    if pay.get("currency") != "XTR":
        return
    user, stars, charge = msg["from"], int(pay["total_amount"]), pay.get("telegram_payment_charge_id")
    coins = round(stars * STAR_RATE, 2)
    st = {"applied": False}

    def mut(users):
        rec = find(users, user["id"])
        if rec is None:
            rec = new_rec(user)
            users.append(rec)
        paid = rec.setdefault("paid", [])
        if charge in paid:        # платёж уже зачислен
            return {"skip": True}
        rec["grantT"] = round(rec.get("grantT", 0) + coins, 2)
        rec["paid"] = (paid + [charge])[-50:]
        st["applied"] = True
        return {"ids": [user["id"]]}
    cloud_update(mut, tries=6)
    if st["applied"]:
        tg("sendMessage", chat_id=msg["chat"]["id"], text=f"✅ Пополнение: {stars} ⭐ → +{coins} BC. Монеты уже в игре!")


def grant_share(user, via_inline):
    """Отправка сообщения друзьям подтверждена: одно разблокирование в 12 часов."""
    st = {"granted": False, "recent": False}

    def mut(users):
        rec = find(users, user["id"])
        if rec is None:
            rec = new_rec(user)
            users.append(rec)
        t = now_ms()
        last = rec.get("lastShare", 0)
        if t - last >= SHARE_COOLDOWN_MS:
            rec["lastShare"] = t
            if via_inline:                       # через игру кредит выдаёт сама игра (в ответе), через inline — копим счётчик
                rec["shareT"] = rec.get("shareT", 0) + 1
            st["granted"] = True
            return {"ids": [user["id"]]}
        if t - last < 120000 and not st["granted"]:
            st["recent"] = True                  # то же самое отправление, что уже учли
        return {"skip": True}
    cloud_update(mut)
    return st["granted"] or st["recent"]


# ---------- API для игры ----------
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
        "description": f"{stars} ⭐ = {coins} BC",
        "payload": f"topup_{stars}",
        "currency": "XTR",            # Telegram Stars
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
    sub = None   # None = проверить не удалось (бот не админ канала)
    try:
        m = http.get(f"{TELEGRAM_API_URL}/getChatMember", params={"chat_id": NEWS_CHAT, "user_id": user["id"]}, timeout=10).json()
        if m.get("ok"):
            sub = m["result"]["status"] in ("creator", "administrator", "member", "restricted")
    except Exception as e:
        print("getChatMember error:", e)
    touch_user(user)   # заодно запоминаем игрока
    return jsonify({"nick": BOT_NICK in name, "sub": sub})


def share_result(uid):
    """Сообщение для друзей: картинка сверху, текст, кнопка входа с реф-ссылкой отправителя."""
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    return {
        "type": "photo", "id": "share",
        "photo_url": SHARE_IMAGE, "thumbnail_url": SHARE_THUMB, "photo_width": 1280, "photo_height": 714,
        "title": "🎁 Бесплатный кейс", "description": "Отправь друзьям и забери кейс",
        "caption": SHARE_TEXT,
        "reply_markup": {"inline_keyboard": [[{"text": "🎁 Забрать бесплатный кейс", "url": link, "style": "success"}]]},
    }


def share_article(uid):
    """Запасной вариант без картинки (если картинка недоступна Telegram)."""
    return {"type": "article", "id": "share", "title": "🎁 Бесплатный кейс", "description": "Отправь друзьям и забери кейс",
            "input_message_content": {"message_text": SHARE_TEXT}, "reply_markup": share_result(uid)["reply_markup"]}


def prepare_message(uid, result):
    return http.post(f"{TELEGRAM_API_URL}/savePreparedInlineMessage", json={
        "user_id": uid, "result": result,
        "allow_user_chats": True, "allow_group_chats": True, "allow_bot_chats": False, "allow_channel_chats": False,
    }, timeout=10).json()


@app.route('/api/share', methods=['POST'])
def api_share():
    """Игра просит подготовить сообщение — потом Telegram показывает «Отправить сообщение» с выбором чата (shareMessage)."""
    data = request.get_json(silent=True) or {}
    user = verify_init_data(data.get("initData"))
    if not user:
        return jsonify({"error": "unauthorized"}), 403
    photo = True
    r = prepare_message(user["id"], share_result(user["id"]))
    if not r.get("ok"):                      # чаще всего — недоступна картинка: пробуем без неё
        print("savePreparedInlineMessage(photo) failed:", r)
        photo = False
        r = prepare_message(user["id"], share_article(user["id"]))
    if not r.get("ok"):
        print("savePreparedInlineMessage(article) failed:", r)
        return jsonify({"error": r.get("description", "telegram error")}), 502
    return jsonify({"id": r["result"]["id"], "photo": photo})


@app.route('/api/health')
def api_health():
    """Открой в браузере https://ТВОЙ-СЕРВИС.onrender.com/api/health — покажет, что работает, а что нет."""
    out = {"bot": "ok", "banner": None, "npoint_records": None}
    try:
        out["banner"] = http.get(SHARE_IMAGE, timeout=8).status_code     # 200 = картинка на месте
    except Exception as e:
        out["banner"] = str(e)[:80]
    try:
        out["npoint_records"] = len(load_users())
    except Exception as e:
        out["npoint_records"] = str(e)[:80]
    return jsonify(out)


@app.route('/api/shared', methods=['POST'])
def api_shared():
    """Telegram сообщил игре, что сообщение отправлено — разрешаем бесплатный кейс (не чаще раза в 12 часов)."""
    data = request.get_json(silent=True) or {}
    user = verify_init_data(data.get("initData"))
    if not user:
        return jsonify({"error": "unauthorized"}), 403
    try:
        return jsonify({"granted": grant_share(user, via_inline=False)})
    except Exception as e:
        print("share error:", e)
        return jsonify({"error": "db"}), 503


# ---------- Telegram ----------
@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    update = request.get_json(silent=True) or {}
    if "pre_checkout_query" in update:   # оплата звёздами: подтверждаем счёт сразу
        tg("answerPreCheckoutQuery", pre_checkout_query_id=update["pre_checkout_query"]["id"], ok=True)
    else:
        # всё остальное — в фоне: Telegram получает ответ мгновенно, бот не «тормозит»
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
                {"text": "📰 Новости", "url": NEWS_URL},
                {"text": "💬 Поддержка", "url": SUPPORT_URL}
            ],
            [
                {"text": "🎁 Ввести бонус-код", "callback_data": "promo"}
            ]
        ]
    }
    tg("sendMessage", chat_id=chat_id,
       text="Привет! 🔥\n\nДобро пожаловать в BRAINROT CASE!\nНажми кнопку ниже, чтобы открыть приложение, крутить кейсы и фармить BC!",
       reply_markup=keyboard)


def ask_promo(chat_id):
    # force_reply: ответ пользователя приходит как reply на это сообщение — работает и после перезапуска сервера
    tg("sendMessage", chat_id=chat_id, text=PROMO_PROMPT,
       reply_markup={"force_reply": True, "input_field_placeholder": "Бонус-код"})


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
        send_welcome(chat_id)            # сначала отвечаем — игрок не ждёт базу
        if rest.startswith("ref_"):
            try:
                register_referral(user, rest)
            except Exception as e:
                print("referral error:", e)
    elif cmd == "/promo":
        try_redeem(chat_id, user, rest) if rest else ask_promo(chat_id)
    elif text and not text.startswith("/") and PROMO_PROMPT in ((msg.get("reply_to_message") or {}).get("text") or ""):
        try_redeem(chat_id, user, text)


def on_inline_query(q):
    """Запасной путь: сообщение для друзей через выбор чата в самом Telegram."""
    tg("answerInlineQuery", inline_query_id=q["id"], results=[share_result(q["from"]["id"])], cache_time=0, is_personal=True)


def on_chosen(c):
    if c.get("result_id") != "share":
        return
    user = c["from"]
    ok = grant_share(user, via_inline=True)
    tg("sendMessage", chat_id=user["id"],
       text="✅ Сообщение отправлено! Бесплатный кейс ждёт тебя в игре." if ok
       else "Ты уже получал кейс за отправку — следующий через 12 часов.")


def on_callback(q):
    tg("answerCallbackQuery", callback_query_id=q["id"])
    if q.get("data") == "promo":
        ask_promo(q["message"]["chat"]["id"])


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
        try:   # кнопка меню чата с ботом тоже открывает игру с адресом бота (?api=...)
            http.post(f"{TELEGRAM_API_URL}/setChatMenuButton", json={"menu_button": {"type": "web_app", "text": "Играть", "web_app": {"url": game_url()}}}, timeout=10)
        except Exception as e:
            print("menu button error:", e)
        threading.Thread(target=keep_alive, daemon=True).start()

    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port, threaded=True)