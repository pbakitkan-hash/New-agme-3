import os
import hmac
import hashlib
import json
import time
import copy
import re
import signal
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

# --- ссылки, база игроков (npoint — только для сохранения), промокоды, звёзды ---
NPOINT_URL = os.environ.get('NPOINT_URL', 'https://api.npoint.io/2816c8000051e7c162ec')
OWNER_ID = int(os.environ.get('OWNER_ID', '6638127989'))
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
WELCOME_TEXT = "Привет! 🔥\n\nДобро пожаловать в BRAINROT CASE!\nНажми кнопку ниже, чтобы открыть приложение, крутить кейсы и фармить BC!"
DEFAULT_PROMOS = {"Uuu": {"reward": 100, "usedBy": []}, "ADMIN1000": {"reward": 1000, "usedBy": []}}
PROMO_USER_REWARD = 50           # личный промокод игрока: друг получает
PROMO_OWNER_REWARD = 25          # ...а владелец кода получает
SNAP_KEYS = ("score", "energy", "level", "clicks", "freeAt", "nickAt", "subAt", "shareCredits", "refTickets",
             "inv", "ts", "grantSeen", "tkSeen", "shareSeen", "giftSeen", "resetSeen", "clearSeen")

app = Flask(__name__)
CORS(app)

http = requests.Session()        # одно соединение на все запросы — быстрее
http.mount('https://', requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=30))
known_ids = set()
CHECK = {}                       # кэш проверки подписки (45 сек) — не дёргаем Telegram каждый раз
PREP = {}                        # готовое сообщение для друзей (10 мин) — окно отправки открывается мгновенно
users_db = {}

# ---------- ВСЕ данные игроков живут в памяти бота (мгновенно); npoint — только копия на диск ----------
state_lock = threading.RLock()
USERS = {}                       # id -> запись игрока
PROMOS = {}                      # код -> {reward, usedBy, [owner, ownerReward]}
FEED = []                        # общая лента дропов (LIVE)
META = {"closed": False}         # сервер закрыт для всех (кроме владельца)
S = {"loaded": False, "dirty": False, "last_save": 0.0, "empty_seen": False, "feedV": 0}


def now_ms():
    return int(time.time() * 1000)


def mark():
    S["dirty"] = True


def migrate(rec):
    """Старые поля (grant/tkAdd/shareAdd/gifts без номера) → накопительные счётчики."""
    g = rec.pop("grant", 0)
    if g:
        rec["grantT"] = round(rec.get("grantT", 0) + g, 2)
    t = rec.pop("tkAdd", 0)
    if t:
        rec["tkT"] = rec.get("tkT", 0) + t
    if rec.pop("shareAdd", 0):
        rec["shareT"] = rec.get("shareT", 0) + 1
    for gift in rec.get("gifts") or []:
        if not gift.get("n"):
            rec["giftN"] = rec.get("giftN", 0) + 1
            gift["n"] = rec["giftN"]
    return rec


def read_npoint():
    r = http.get(NPOINT_URL, timeout=12)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError("npoint вернул не список")
    return data


def save_npoint(arr):
    # npoint обновляет документ методом POST; если не принял — пробуем PUT
    last = None
    for send in (http.post, http.put):
        try:
            r = send(NPOINT_URL, json=arr, timeout=12)
            if r.ok:
                return
            last = r.status_code
        except Exception as e:
            last = e
    raise RuntimeError(f"npoint: запись не удалась ({last})")


def load_state():
    data = read_npoint()
    users, promos, feed, meta = {}, None, [], {}
    for e in data:
        if isinstance(e, dict) and e.get("id") == "__promos__":
            promos = e.get("codes") or {}
        elif isinstance(e, dict) and e.get("id") == "__feed__":
            feed = e.get("items") or []
        elif isinstance(e, dict) and e.get("id") == "__meta__":
            meta = e
        elif isinstance(e, dict) and isinstance(e.get("id"), int):
            users[e["id"]] = migrate(e)
    if not users and not S["empty_seen"]:       # пустой ответ может быть сбоем — перепроверим один раз
        S["empty_seen"] = True
        raise RuntimeError("пустая база — перепроверяю")
    with state_lock:
        USERS.clear()
        USERS.update(users)
        PROMOS.clear()
        PROMOS.update(promos if promos is not None else copy.deepcopy(DEFAULT_PROMOS))
        FEED[:] = feed
        META["closed"] = bool(meta.get("closed"))
        S["loaded"] = True
        S["dirty"] = promos is None
    print(f"база загружена: {len(USERS)} игроков, {len(PROMOS)} промокодов")


def flush_now():
    with state_lock:
        arr = [{"id": "__promos__", "codes": copy.deepcopy(PROMOS)}, {"id": "__feed__", "items": list(FEED)}, {"id": "__meta__", "closed": META["closed"]}] \
              + [copy.deepcopy(u) for u in USERS.values()]
        S["dirty"] = False
    try:
        save_npoint(arr)
        S["last_save"] = time.time()
    except Exception as e:
        S["dirty"] = True
        print("сохранение не удалось, повторю:", e)


def persist_loop():
    while True:
        time.sleep(1)
        try:
            if not S["loaded"]:
                load_state()
            elif (S["dirty"] and time.time() - S["last_save"] >= 2.5) or time.time() - S["last_save"] > 60:
                flush_now()          # изменения уходят на диск за ~3 секунды, а раз в минуту — для надёжности
        except Exception as e:
            print("база:", e)
            time.sleep(1.5)


def wait_loaded(timeout=8.0):
    t = time.time()
    while not S["loaded"] and time.time() - t < timeout:
        time.sleep(0.1)
    return S["loaded"]


def game_url():
    """Ссылка на игру: бот сам передаёт игре свой адрес (?api=...), v= сбрасывает старый кэш."""
    api = os.environ.get('RENDER_EXTERNAL_URL', '')
    if not api:
        return WEB_APP_URL
    sep = '&' if '?' in WEB_APP_URL else '?'
    return f"{WEB_APP_URL}{sep}api={quote(api, safe='')}&v=7"


@app.route('/')
def home():
    return "BRAINROT CASE Backend is running!"


@app.route('/ping')
def ping():
    # лёгкий адрес для cron-job: отвечает мгновенно и не трогает базу
    return "pong"


@app.route('/api/health')
def api_health():
    """Открой в браузере https://ТВОЙ-СЕРВИС.onrender.com/api/health — покажет, что работает, а что нет."""
    out = {"bot": "ok", "loaded": S["loaded"], "closed": META["closed"], "players": len(USERS), "promos": len(PROMOS), "banner": None}
    try:
        out["banner"] = http.get(SHARE_IMAGE, timeout=8).status_code     # 200 = картинка на месте
    except Exception as e:
        out["banner"] = str(e)[:80]
    return jsonify(out)


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


# ---------- работа с записями игроков ----------
def name_of(user):
    return user.get("username") or user.get("first_name") or "Player"


def get_rec(user):
    """Запись игрока (создаётся, если её нет). Вызывать под state_lock."""
    rec = USERS.get(user["id"])
    if rec is None:
        t = now_ms()
        rec = USERS[user["id"]] = {"id": user["id"], "username": name_of(user), "score": 0, "first": t, "seen": t}
        mark()
    return rec


def compute_inc(rec, seen):
    """Что игрок ещё не забрал: счётчики растут только вверх, игра хранит «забрал до …» — повтор запроса безопасен."""
    seen = seen if isinstance(seen, dict) else {}
    isn = lambda k: int(seen.get(k) or 0)
    g, t, s, n = rec.get("grantT", 0), rec.get("tkT", 0), rec.get("shareT", 0), rec.get("giftN", 0)
    rs, cs = rec.get("resetT", 0), rec.get("clearT", 0)
    return {"grantT": g, "tkT": t, "shareT": s, "giftN": n, "resetT": rs, "clearT": cs,
            "grant": round(g - float(seen.get("grantSeen") or 0), 2), "tk": t - isn("tkSeen"),
            "share": s > isn("shareSeen"), "reset": rs > isn("resetSeen"), "clear": cs > isn("clearSeen"),
            "gifts": [x for x in (rec.get("gifts") or []) if x.get("n", 0) > isn("giftSeen")]}


def my_promos(uid):
    return [{"code": k, "reward": v.get("reward", 0), "max": v.get("max", 0), "used": len(v.get("usedBy", []))}
            for k, v in PROMOS.items() if v.get("owner") == uid]


def find_code(code):
    """Промокод без учёта регистра."""
    if code in PROMOS:
        return code
    low = code.lower()
    return next((k for k in PROMOS if k.lower() == low), None)


def extras(user, rec, data):
    out = {"refCount": rec.get("refCount", 0), "myPromos": my_promos(user["id"]), "closed": META["closed"]}
    if data.get("feedV") != S["feedV"]:
        out["feed"] = FEED[-20:][::-1]
        out["feedV"] = S["feedV"]
    return out


def clean_snap(s):
    out = {}
    for k in SNAP_KEYS:
        v = (s or {}).get(k)
        if k == "inv":
            if isinstance(v, list) and len(v) <= 500:
                out[k] = v
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) < 1e15:
            out[k] = v
    return out


def redeem_code(user, code):
    """Промокод (из игры и из чата): мгновенно, в памяти. Возвращает {"reward": n} или {"err": текст}."""
    uid = user["id"]
    with state_lock:
        key = find_code(code)
        if key is None:
            return {"err": "Промокод не найден"}
        info = PROMOS[key]
        if info.get("owner") == uid:
            return {"err": "Нельзя использовать собственный код"}
        if uid in info.get("usedBy", []):
            return {"err": "Вы уже использовали этот промокод"}
        if info.get("max") and len(info.get("usedBy", [])) >= info["max"]:
            return {"err": "Промокод закончился"}
        rec = get_rec(user)
        rec["grantT"] = round(rec.get("grantT", 0) + info["reward"], 2)
        info.setdefault("usedBy", []).append(uid)
        owner = USERS.get(info["owner"]) if info.get("owner") else None
        if owner:                      # владельцу промокода — билет (и награда, если она задана)
            owner["grantT"] = round(owner.get("grantT", 0) + info.get("ownerReward", 0), 2)
            owner["tkT"] = owner.get("tkT", 0) + 1
            owner["refCount"] = owner.get("refCount", 0) + 1
        mark()
        return {"reward": info["reward"]}


def touch_user(user):
    """Запоминаем любого, кто зашёл."""
    uid = user.get("id")
    if not uid or uid in known_ids or not S["loaded"]:
        return
    with state_lock:
        get_rec(user)
    known_ids.add(uid)


def register_referral(user, payload):
    """/start ref_<id>: засчитываем только совсем нового игрока и только один раз."""
    inviter_id = int(payload.split("_", 1)[1])
    uid = user["id"]
    if inviter_id == uid or not wait_loaded():
        return
    with state_lock:
        inviter = USERS.get(inviter_id)
        if uid in USERS or inviter is None:
            return
        rec = get_rec(user)
        rec["ref"] = inviter_id
        inviter["tkT"] = inviter.get("tkT", 0) + 1
        inviter["refCount"] = inviter.get("refCount", 0) + 1
        mark()
    known_ids.add(uid)
    tg("sendMessage", chat_id=inviter_id,
       text=f"🎉 По вашей ссылке пришёл @{name_of(user)}! Вам начислен 🎟️ билет на реферальный кейс.")


def grant_share(user, via_inline):
    """Отправка сообщения друзьям подтверждена: засчитываем, если бесплатный кейс сейчас не на перезарядке."""
    with state_lock:
        rec = get_rec(user)
        t = now_ms()
        if t >= rec.get("freeAt", 0):
            rec["lastShare"] = t
            if via_inline:               # через игру кредит выдаёт сама игра (в ответе), через inline — копим счётчик
                rec["shareT"] = rec.get("shareT", 0) + 1
            mark()
            return True
        return False


# ---------- проверка подписи Telegram ----------
def verify_init_data(init_data):
    """Возвращает данные пользователя или None."""
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


def auth():
    """(данные запроса, пользователь, ошибка-ответ)."""
    data = request.get_json(silent=True) or {}
    user = verify_init_data(data.get("initData"))
    if not user:
        return data, None, (jsonify({"error": "unauthorized"}), 403)
    if not S["loaded"]:
        return data, user, (jsonify({"error": "loading"}), 503)
    if META["closed"] and user["id"] != OWNER_ID and request.path not in ("/api/sync", "/api/poll"):
        return data, user, (jsonify({"error": "closed", "closed": True}), 503)
    return data, user, None


# ---------- API для игры (всё в памяти — отвечает за миллисекунды) ----------
@app.route('/api/sync', methods=['POST'])
def api_sync():
    data, user, err = auth()
    if err:
        return err
    snap = clean_snap(data.get("snap"))
    with state_lock:
        rec = get_rec(user)
        rec["username"], rec["seen"] = name_of(user), now_ms()
        out = {"ok": True}
        adopt = False
        if data.get("first"):            # первый вход с этого устройства: берём из базы, если у нас пусто или там свежее
            if rec.get("inv") is not None and (not data.get("hadLocal") or rec.get("ts", 0) > snap.get("ts", 0)):
                adopt = True
                out["adopt"] = True
                out["snap"] = {k: rec[k] for k in SNAP_KEYS if k in rec}
            elif rec.get("inv") is None and rec.get("score", 0) > snap.get("score", 0):
                out["adoptScore"] = rec["score"]      # запись из очень старой версии: был только баланс
                snap.pop("score", None)
        if not adopt and snap:
            if snap.get("resetSeen", 0) < rec.get("resetT", 0):    # админ сбросил баланс, а игра ещё не знает — не затираем сброс
                snap.pop("score", None)
            if snap.get("clearSeen", 0) < rec.get("clearT", 0):
                snap.pop("inv", None)
            if any(rec.get(k) != v for k, v in snap.items()):
                rec.update(snap)
                mark()
        out["inc"] = compute_inc(rec, rec if adopt else snap)
        out.update(extras(user, rec, data))
        return jsonify(out)


@app.route('/api/poll', methods=['POST'])
def api_poll():
    data, user, err = auth()
    if err:
        return err
    with state_lock:
        rec = get_rec(user)
        rec["seen"] = now_ms()
        out = {"inc": compute_inc(rec, data.get("seen"))}
        out.update(extras(user, rec, data))
        return jsonify(out)


@app.route('/api/promo', methods=['POST'])
def api_promo():
    data, user, err = auth()
    if err:
        return err
    res = redeem_code(user, str(data.get("code", "")).strip())
    if "err" in res:
        return jsonify(res)
    with state_lock:
        res["inc"] = compute_inc(USERS[user["id"]], data.get("seen"))
    return jsonify(res)


@app.route('/api/promo/new', methods=['POST'])
def api_promo_new():
    """Любой игрок создаёт промокод за свои монеты: монеты × активации спишутся, остаток вернётся при удалении."""
    data, user, err = auth()
    if err:
        return err
    code = str(data.get("code", "")).strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3,16}", code):
        return jsonify({"err": "Код: 3–16 символов, латиница и цифры"})
    try:
        reward, mx = round(float(data.get("reward")), 2), int(data.get("max"))
    except Exception:
        return jsonify({"err": "Заполните монеты и число активаций"})
    if not (1 <= reward <= 1000000) or not (1 <= mx <= 1000):
        return jsonify({"err": "Монеты: 1–1 000 000, активации: 1–1000"})
    cost = round(reward * mx, 2)
    with state_lock:
        rec = get_rec(user)
        if len(my_promos(user["id"])) >= 5:
            return jsonify({"err": "Не больше 5 своих промокодов"})
        if find_code(code):
            return jsonify({"err": "Такой код уже занят"})
        have = rec.get("score", 0) + rec.get("grantT", 0) - rec.get("grantSeen", 0)
        if have < cost:
            return jsonify({"err": f"Не хватает монет: нужно {cost} BC"})
        rec["grantT"] = round(rec.get("grantT", 0) - cost, 2)
        PROMOS[code] = {"reward": reward, "max": mx, "owner": user["id"], "ownerReward": 0, "usedBy": [], "funded": True}
        mark()
        return jsonify({"ok": True, "inc": compute_inc(rec, data.get("seen")), "myPromos": my_promos(user["id"])})


@app.route('/api/promo/del', methods=['POST'])
def api_promo_del():
    data, user, err = auth()
    if err:
        return err
    with state_lock:
        key = find_code(str(data.get("code", "")).strip())
        info = PROMOS.get(key) if key else None
        if not info or info.get("owner") != user["id"]:
            return jsonify({"err": "Промокод не найден"})
        rec = get_rec(user)
        if info.get("funded"):          # возвращаем монеты за неиспользованные активации
            rec["grantT"] = round(rec.get("grantT", 0) + info.get("reward", 0) * max(0, info.get("max", 0) - len(info.get("usedBy", []))), 2)
        del PROMOS[key]
        mark()
        return jsonify({"ok": True, "inc": compute_inc(rec, data.get("seen")), "myPromos": my_promos(user["id"])})


@app.route('/api/profile', methods=['POST'])
def api_profile():
    """Профиль игрока из ленты LIVE: баланс, брейнроты, история дропов."""
    data, user, err = auth()
    if err:
        return err
    try:
        uid = int(data.get("id"))
    except Exception:
        return jsonify({"err": "Игрок не найден"})
    with state_lock:
        rec = USERS.get(uid)
        if rec is None:
            return jsonify({"err": "Игрок не найден"})
        inv = rec.get("inv") or []
        return jsonify({"id": uid, "username": rec.get("username"),
                        "balance": round(rec.get("score", 0) + rec.get("grantT", 0) - rec.get("grantSeen", 0), 2),
                        "items": sum(g.get("count", 1) for g in inv),
                        "inv": sorted(inv, key=lambda g: -g.get("sellPrice", 0))[:40], "drops": (rec.get("drops") or [])[:20]})


@app.route('/api/drop', methods=['POST'])
def api_drop():
    """Игрок сообщает о выпавших брейнротах — лента LIVE общая для всех."""
    data, user, err = auth()
    if err:
        return err
    with state_lock:
        rec = get_rec(user)
        for d in (data.get("drops") or [])[:10]:
            try:
                row = {"n": str(d["n"])[:40], "i": str(d.get("i") or "🎁")[:8], "p": float(d["p"]), "c": str(d.get("c", ""))[:30], "t": now_ms()}
            except Exception:
                continue
            FEED.append({"u": name_of(user), "id": user["id"], **row})
            rec["drops"] = ([row] + (rec.get("drops") or []))[:30]          # личная история дропов игрока
        del FEED[:-60]
        S["feedV"] += 1
        mark()
    return jsonify({"ok": True})


def owner_only():
    data, user, err = auth()
    if err:
        return data, None, err
    if user["id"] != OWNER_ID:
        return data, None, (jsonify({"error": "forbidden"}), 403)
    return data, user, None


def resolve(t):
    """Игрок по @username или по ID."""
    t = str(t or "").strip()
    if not t:
        return None
    if t.isdigit():
        return USERS.get(int(t))
    low = t.lstrip("@").lower()
    return next((u for u in USERS.values() if (u.get("username") or "").lower() == low), None)


@app.route('/api/admin', methods=['POST'])
def api_admin():
    """Панель владельца: выдать/сбросить/очистить/закрыть сервер/промокоды."""
    data, user, err = owner_only()
    if err:
        return err
    op = data.get("op")
    with state_lock:
        if op == "users":
            rows = []
            for u in USERS.values():
                inv = u.get("inv") or []
                rows.append({"id": u["id"], "username": u.get("username"), "seen": u.get("seen", 0),
                             "balance": round(u.get("score", 0) + u.get("grantT", 0) - u.get("grantSeen", 0), 2),
                             "items": sum(g.get("count", 1) for g in inv), "inv": inv[:6]})
            rows.sort(key=lambda r: r["seen"], reverse=True)
            return jsonify({"users": rows, "total": len(rows), "closed": META["closed"]})
        if op == "promo_list":
            return jsonify({"codes": copy.deepcopy(PROMOS), "closed": META["closed"]})
        if op == "close":
            META["closed"] = not META["closed"]
            mark()
            return jsonify({"log": "сервер закрыт для всех" if META["closed"] else "сервер открыт", "closed": META["closed"]})
        if op == "clearall":
            for r in USERS.values():
                r["clearT"], r["inv"] = r.get("clearT", 0) + 1, []
            mark()
            return jsonify({"log": f"брейнроты очищены у всех ({len(USERS)})"})
        if op == "resetall":
            for r in USERS.values():
                r["resetT"], r["score"] = r.get("resetT", 0) + 1, 0
            mark()
            return jsonify({"log": f"баланс сброшен у всех ({len(USERS)})"})
        if op in ("promo_create", "promo_delete", "promo_keep"):
            code = str(data.get("code", "")).strip()
            if op == "promo_create":
                try:
                    reward, mx = float(data.get("reward")), int(data.get("max") or 0)
                except Exception:
                    return jsonify({"err": "Заполните код и монеты"})
                if not re.fullmatch(r"[A-Za-z0-9]{3,16}", code) or reward <= 0:
                    return jsonify({"err": "Код: 3–16 символов (a-z, 0-9), монеты больше нуля"})
                PROMOS[code] = {"reward": reward, "max": max(0, mx), "usedBy": []}
                log = f"промо {code}: {reward} BC × {mx or '∞'}"
            elif op == "promo_delete":
                PROMOS.pop(find_code(code) or code, None)
                log = f"промо {code} удалён"
            else:
                keep = find_code(code) if code else (next(iter(PROMOS), None))
                if keep is None:
                    return jsonify({"err": "Такого промокода нет"})
                PROMOS.clear()
                PROMOS[keep] = {"reward": 0, "usedBy": []} if keep not in PROMOS else PROMOS[keep]
                log = f"остался только {keep}"
            mark()
            return jsonify({"log": log, "codes": copy.deepcopy(PROMOS)})
        rec = resolve(data.get("target"))
        if rec is None:
            return jsonify({"err": "Игрок не найден (он должен хотя бы раз открыть игру)"})
        who = "@" + (rec.get("username") or str(rec["id"]))
        if op == "grant":
            try:
                amount = float(data.get("amount"))
            except Exception:
                return jsonify({"err": "Укажите сумму"})
            rec["grantT"] = round(rec.get("grantT", 0) + amount, 2)
            log = f"{amount:+g} BC → {who}"
        elif op == "reset":
            rec["resetT"], rec["score"] = rec.get("resetT", 0) + 1, 0
            log = f"баланс сброшен → {who}"
        elif op == "clear":
            rec["clearT"], rec["inv"] = rec.get("clearT", 0) + 1, []
            log = f"брейнроты очищены → {who}"
        elif op == "give":
            try:
                qty, price = max(1, min(1000, int(data.get("qty") or 1))), float(data.get("price"))
            except Exception:
                return jsonify({"err": "Неверные данные предмета"})
            rec["giftN"] = rec.get("giftN", 0) + 1
            rec["gifts"] = ((rec.get("gifts") or []) + [{"n": rec["giftN"], "name": str(data.get("name", ""))[:40], "icon": str(data.get("icon") or "🎁")[:8],
                                                          "sellPrice": price, "count": qty}])[-50:]
            log = f"{qty}× {data.get('name')} → {who}"
        else:
            return jsonify({"err": "Неизвестная команда"})
        mark()
        return jsonify({"log": log})


@app.route('/api/invoice', methods=['POST'])
def api_invoice():
    data, user, err = auth()
    if err:
        return err
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
    data, user, err = auth()
    if err:
        return err
    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".lower()
    sub = None   # None = проверить не удалось (бот не админ канала)
    cached = CHECK.get(user["id"])
    if cached and time.time() - cached[0] < 45:
        sub = cached[1]
    else:
        try:
            m = http.get(f"{TELEGRAM_API_URL}/getChatMember", params={"chat_id": NEWS_CHAT, "user_id": user["id"]}, timeout=6).json()
            if m.get("ok"):
                sub = m["result"]["status"] in ("creator", "administrator", "member", "restricted")
        except Exception as e:
            print("getChatMember error:", e)
        CHECK[user["id"]] = (time.time(), sub)
    touch_user(user)
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
    """Игра просит подготовить сообщение — потом Telegram показывает «Отправить сообщение» с выбором чата."""
    data, user, err = auth()
    if err:
        return err
    cached = PREP.get(user["id"])
    if cached and time.time() - cached[0] < 600:
        return jsonify({"id": cached[1], "photo": cached[2]})
    photo = True
    r = prepare_message(user["id"], share_result(user["id"]))
    if not r.get("ok"):                      # чаще всего — недоступна картинка: пробуем без неё
        print("savePreparedInlineMessage(photo) failed:", r)
        photo = False
        r = prepare_message(user["id"], share_article(user["id"]))
    if not r.get("ok"):
        print("savePreparedInlineMessage(article) failed:", r)
        return jsonify({"error": r.get("description", "telegram error")}), 502
    PREP[user["id"]] = (time.time(), r["result"]["id"], photo)
    return jsonify({"id": r["result"]["id"], "photo": photo})


@app.route('/api/shared', methods=['POST'])
def api_shared():
    """Telegram сообщил игре, что сообщение отправлено — разрешаем бесплатный кейс (не чаще раза в 12 часов)."""
    data, user, err = auth()
    if err:
        return err
    granted = grant_share(user, via_inline=False)
    with state_lock:
        return jsonify({"granted": granted, "shareT": get_rec(user).get("shareT", 0)})


# ---------- Telegram ----------
def welcome_payload(chat_id):
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
    return {"method": "sendMessage", "chat_id": chat_id, "text": WELCOME_TEXT, "reply_markup": keyboard}


@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    update = request.get_json(silent=True) or {}
    if "pre_checkout_query" in update:   # оплата звёздами: подтверждаем счёт прямо в ответе
        return jsonify({"method": "answerPreCheckoutQuery", "pre_checkout_query_id": update["pre_checkout_query"]["id"], "ok": True})
    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    if text and text.split(" ", 1)[0].split("@")[0] == "/start":
        threading.Thread(target=process_update, args=(update, True), daemon=True).start()
        return jsonify(welcome_payload(msg["chat"]["id"]))     # приветствие — прямо в ответе вебхука: самый быстрый путь
    threading.Thread(target=process_update, args=(update, False), daemon=True).start()
    return "OK", 200


def process_update(update, welcomed):
    try:
        if "message" in update:
            on_message(update["message"], welcomed)
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


def ask_promo(chat_id):
    # force_reply: ответ пользователя приходит как reply на это сообщение — работает и после перезапуска сервера
    tg("sendMessage", chat_id=chat_id, text=PROMO_PROMPT,
       reply_markup={"force_reply": True, "input_field_placeholder": "Бонус-код"})


def try_redeem(chat_id, user, code):
    if not wait_loaded():
        return tg("sendMessage", chat_id=chat_id, text="⏳ Бот только проснулся — повторите код через несколько секунд.")
    res = redeem_code(user, code.strip())
    tg("sendMessage", chat_id=chat_id,
       text=f"✅ Бонус-код принят! +{res['reward']} BC. Монеты уже начисляются в игре." if "reward" in res else f"❌ {res['err']}.")


def promo_exists(text):
    with state_lock:
        return text in PROMOS or text.upper() in PROMOS


def on_payment(msg):
    pay = msg["successful_payment"]
    if pay.get("currency") != "XTR" or not wait_loaded():
        return
    user, stars, charge = msg["from"], int(pay["total_amount"]), pay.get("telegram_payment_charge_id")
    coins = round(stars * STAR_RATE, 2)
    with state_lock:
        rec = get_rec(user)
        paid = rec.setdefault("paid", [])
        if charge in paid:        # платёж уже зачислен
            return
        rec["grantT"] = round(rec.get("grantT", 0) + coins, 2)
        rec["paid"] = (paid + [charge])[-50:]
        mark()
    tg("sendMessage", chat_id=msg["chat"]["id"], text=f"✅ Пополнение: {stars} ⭐ → +{coins} BC. Монеты уже в игре!")


def on_message(msg, welcomed=False):
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
        if not welcomed:
            payload = welcome_payload(chat_id)
            payload.pop("method")
            tg("sendMessage", **payload)
        if rest.startswith("ref_"):
            try:
                register_referral(user, rest)
            except Exception as e:
                print("referral error:", e)
    elif cmd == "/promo":
        try_redeem(chat_id, user, rest) if rest else ask_promo(chat_id)
    elif text and not text.startswith("/"):
        replied = PROMO_PROMPT in ((msg.get("reply_to_message") or {}).get("text") or "")
        if replied or (S["loaded"] and promo_exists(text)):     # код можно просто написать боту — без кнопок
            try_redeem(chat_id, user, text)


def on_inline_query(q):
    """Запасной путь: сообщение для друзей через выбор чата в самом Telegram."""
    tg("answerInlineQuery", inline_query_id=q["id"], results=[share_result(q["from"]["id"])], cache_time=0, is_personal=True)


def on_chosen(c):
    if c.get("result_id") != "share" or not wait_loaded():
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


def start_background():
    threading.Thread(target=persist_loop, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()

    def on_term(*_):          # Render останавливает сервис при обновлении: успеваем сохранить всё из памяти
        if S["loaded"]:
            flush_now()
        os._exit(0)
    try:
        signal.signal(signal.SIGTERM, on_term)
    except Exception:
        pass


if __name__ == '__main__':
    start_background()
    render_url = os.environ.get('RENDER_EXTERNAL_URL')
    if render_url:
        http.post(f"{TELEGRAM_API_URL}/setWebhook", json={
            "url": f"{render_url}/{TOKEN}",
            "allowed_updates": ["message", "callback_query", "pre_checkout_query", "inline_query", "chosen_inline_result"],
        }, timeout=10)
        try:   # кнопка меню чата с ботом тоже открывает игру с адресом бота (?api=...)
            http.post(f"{TELEGRAM_API_URL}/setChatMenuButton", json={"menu_button": {"type": "web_app", "text": "Играть", "web_app": {"url": game_url()}}}, timeout=10)
        except Exception as e:
            print("menu button error:", e)

    port = int(os.environ.get('PORT', 8000))
    try:
        from waitress import serve        # быстрее встроенного сервера Flask (если установлен)
        serve(app, host='0.0.0.0', port=port, threads=16)
    except ImportError:
        app.run(host='0.0.0.0', port=port, threaded=True)
