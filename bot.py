import os
from threading import Thread
from flask import Flask
from telebot import TeleBot, types

# 1. Безопасное получение токена из настроек Render (Environment)
TOKEN = os.environ.get("BOT_TOKEN")

if not TOKEN:
    raise ValueError("ОШИБКА: Переменная BOT_TOKEN не найдена на Render!")

bot = TeleBot(TOKEN)

# Актуальная ссылка на Mini App с обходом кэша
WEB_APP_URL = "https://pbakitkan-hash.github.io/New-agme-3/?v=12"

# 2. Инициализация Flask для работы 24/7 (для Cron-job.org)
app = Flask(__name__)

@app.route('/')
def home():
    return "BRAINROT CASE Bot is running 24/7!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# 3. Обработка команды /start
@bot.message_handler(commands=['start'])
def start_command(message):
    keyboard = types.InlineKeyboardMarkup()
    
    # Кнопка открытия Mini App
    web_app_btn = types.InlineKeyboardButton(
        text="🎮 Играть в BRAINROT CASE", 
        web_app=types.WebAppInfo(url=WEB_APP_URL)
    )
    keyboard.add(web_app_btn)

    name = message.from_user.first_name or "Игрок"
    welcome_text = (
        f"Привет, {name}! 👋\n\n"
        f"🔥 Добро пожаловать в **BRAINROT CASE**!\n\n"
        f"Нажми кнопку ниже, чтобы открыть приложение, крутить кейсы, играть в мины и фармить BC!"
    )

    bot.send_message(
        chat_id=message.chat.id,
        text=welcome_text,
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

# 4. Запуск сервера и бота
if __name__ == '__main__':
    # Запуск веб-сервера в фоновом потоке
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    print("Бот и Flask-сервер успешно запущены!")
    bot.infinity_polling()
