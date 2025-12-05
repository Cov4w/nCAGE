import asyncio
import threading
from telegram import Bot
from telegram.error import TelegramError

class TelegramBot:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.bot = None
        if token and chat_id:
            self.bot = Bot(token=token)
        else:
            print("⚠️ Telegram Token or Chat ID missing. Bot disabled.")

    def _run_async(self, coro):
        """Runs an async coroutine in a separate thread to avoid blocking."""
        def run():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(coro)
                loop.close()
            except Exception as e:
                print(f"⚠️ Telegram Async Error: {e}")

        if self.bot:
            threading.Thread(target=run, daemon=True).start()

    def send_message(self, text):
        """Sends a text message."""
        if not self.bot: return
        
        async def task():
            try:
                await self.bot.send_message(chat_id=self.chat_id, text=text)
                print(f"📨 Telegram Sent: {text}")
            except TelegramError as e:
                print(f"⚠️ Telegram Send Error: {e}")
        
        self._run_async(task())

    def send_image(self, image_path, caption=None):
        """Sends an image."""
        if not self.bot: return

        async def task():
            try:
                with open(image_path, 'rb') as f:
                    await self.bot.send_photo(chat_id=self.chat_id, photo=f, caption=caption)
                print(f"📨 Telegram Image Sent: {image_path}")
            except TelegramError as e:
                print(f"⚠️ Telegram Image Error: {e}")
            except FileNotFoundError:
                print(f"⚠️ Image not found: {image_path}")
        
        self._run_async(task())
