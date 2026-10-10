import os
import threading
from flask import Flask
import requests

# Токен твоего бота
TOKEN = "8830829115:AAE1t8lqMI2ARzog7IPsO5fLKoNptbrw1wI"

# Твоя ссылка на GitHub Pages
WEB_APP_URL = "https://pbakitkan-hash.github.io/New-agme-3/"

# Инициализация Flask для Render Web Service
app = Flask(__name__)

@app.route('/')
def home():
    return "Бот работает 24/7!"

def send_telegram_request(method, payload):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Ошибка запроса: {e}")

def run_bot():
    print("=== БОТ ЗАПУЩЕН ЧЕРЕЗ WEB SERVICE ===")
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={offset}&timeout=30"
            response = requests.get(url, timeout=35)
            data = response.json()

            if data.get("ok"):
                for result in data.get("result", []):
                    offset = result["update_id"] + 1
                    message = result.get("message")
                    
                    if message and "text" in message:
                        chat_id = message["chat"]["id"]
                        text = message["text"]

                        if text == "/start":
                            keyboard = {
                                "inline_keyboard": [[
                                    {
                                        "text": "🎮 Играть в кликер",
                                        "web_app": {"url": WEB_APP_URL}
                                    }
                                ]]
                            }
                            payload = {
                                "chat_id": chat_id,
                                "text": "Привет! Нажми кнопку ниже, чтобы запустить кликер:",
                                "reply_markup": keyboard
                            }
                            send_telegram_request("sendMessage", payload)
        except Exception as e:
            print(f"Ошибка polling: {e}")

if __name__ == "__main__":
    # Запуск фонового потока для бота Telegram
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    # Запуск Flask на сервере Render
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
