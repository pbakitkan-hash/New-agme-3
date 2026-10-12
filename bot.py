import os
from telebot import TeleBot, types

# Безопасное получение токена из переменных окружения Render
TOKEN = os.environ.get("BOT_TOKEN")

if not TOKEN:
    raise ValueError("ОШИБКА: Переменная BOT_TOKEN не найдена на Render!")

bot = TeleBot(TOKEN)

# Актуальная ссылка на Mini App с обходом кэша
WEB_APP_URL = "https://pbakitkan-hash.github.io/New-agme-3/?v=12"

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

if __name__ == '__main__':
    print("Бот успешно запущен и защищен!")
    bot.infinity_polling()
