import os
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

TOKEN = os.environ.get('BOT_TOKEN')
if not TOKEN:
    raise ValueError("ОШИБКА: Переменная BOT_TOKEN не найдена на Render!")

WEB_APP_URL = os.environ.get('WEB_APP_URL', 'https://pbakitkan-hash.github.io/New-agme-3/?v=12')
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TOKEN}"

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
    update = request.get_json()
    if update and "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "")
        if text == "/start":
            send_welcome(chat_id)
    return "OK", 200

def send_welcome(chat_id):
    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "🎮 Играть в BRAINROT CASE",
                    "web_app": {"url": WEB_APP_URL}
                }
            ]
        ]
    }
    payload = {
        "chat_id": chat_id,
        "text": "Привет! 🔥\n\nДобро пожаловать в BRAINROT CASE!\nНажми кнопку ниже, чтобы открыть приложение, крутить кейсы и фармить BC!",
        "reply_markup": keyboard
    }
    requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)

if __name__ == '__main__':
    render_url = os.environ.get('RENDER_EXTERNAL_URL')
    if render_url:
        webhook_url = f"{render_url}/{TOKEN}"
        requests.get(f"{TELEGRAM_API_URL}/setWebhook?url={webhook_url}")

    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
