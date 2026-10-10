from threading import Thread
import time
from flask import Flask
import requests

# Твой новый токен
TOKEN = "8830829115:AAE1t8lqMI2ARzog7IPsO5fLKoNptbrw1wI"

# Укажи ссылку на твои GitHub Pages (если репозиторий новый, измени ссылку на актуальную)
WEB_APP_URL = "https://pbakitkan-hash.github.io/New-agme-2/"

TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"

# 1. Веб-сервер Flask (чтобы Render считал сервис активным)
app = Flask(__name__)


@app.route("/")
def home():
  return "Бот работает 24/7!"


# 2. Функция отправки сообщения с кнопкой Mini App
def send_message(chat_id, first_name):
  url = f"{TELEGRAM_API}/sendMessage"
  payload = {
      "chat_id": chat_id,
      "text": (
          f"Привет, {first_name}!\nНажимай на кнопку ниже, чтобы запустить"
          " кликер:"
      ),
      "reply_markup": {
          "inline_keyboard": [[
              {"text": "🎮 Играть в кликер", "web_app": {"url": WEB_APP_URL}}
          ]]
      },
  }
  try:
    requests.post(url, json=payload)
  except Exception as e:
    print(f"Ошибка при отправке сообщения: {e}")


# 3. Основной цикл опроса Telegram (Polling)
def run_bot():
  offset = 0
  print("=== БОТ ЗАПУЩЕН ЧЕРЕЗ WEB SERVICE ===")
  while True:
    try:
      response = requests.get(
          f"{TELEGRAM_API}/getUpdates",
          params={"offset": offset, "timeout": 30},
      ).json()

      for result in response.get("result", []):
        offset = result["update_id"] + 1
        if "message" in result:
          message = result["message"]
          chat_id = message["chat"]["id"]
          text = message.get("text", "")
          first_name = message["from"].get("first_name", "Игрок")

          if text.startswith("/start"):
            send_message(chat_id, first_name)
    except Exception as e:
      print(f"Ошибка получения обновлений: {e}")
      time.sleep(5)


# 4. Запуск фонового потока бота и Flask-сервера
if __name__ == "__main__":
  bot_thread = Thread(target=run_bot)
  bot_thread.daemon = True
  bot_thread.start()

  # Render передает порт по умолчанию или используем 10000
  app.run(host="0.0.0.0", port=10000)
