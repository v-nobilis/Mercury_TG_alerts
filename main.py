
import ssl
import certifi
import websocket
import websocket
import requests
import json
import time
import threading
import os
import sys
import logging
from decimal import Decimal, getcontext
from dotenv import load_dotenv

# --- Настройка окружения и логирования ---
# Загружаем переменные из.env файла (безопасность)
# Замените строку load_dotenv() на этот блок:
env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_path):
    load_dotenv(env_path)
    print(f"Файл.env найден: {env_path}")
else:
    print("ВНИМАНИЕ: Файл.env не найден! Проверьте имя файла.")

# Настройка логирования, чтобы видеть, что происходит в консоли
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("ArbMonitor")

# Устанавливаем точность для финансовых вычислений
getcontext().prec = 8


# --- Конфигурация ---
class Config:
    # URL вебсокета Binance (Stream: btcusdt@ticker)
    BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@ticker"

    # API Mercuryo (Публичный конвертер)
    MERCURYO_API = "https://api.mercuryo.io/v1.6/public/convert"

    # Параметры запроса к Mercuryo (100 USD -> BTC)
    MERCURYO_PARAMS = {
        "from": "USD",
        "to": "BTC",
        "amount": "100",
        "type": "buy"
    }

    # Telegram настройки (берутся из.env или переменных среды)
    TG_TOKEN = os.getenv("TG_TOKEN")
    TG_CHAT_ID = os.getenv("TG_CHAT_ID")

    # Пороговые значения спреда (в процентах)
    THRESHOLD_LOW = Decimal("0.20")
    THRESHOLD_HIGH = Decimal("0.5")

    # Частота опроса Mercuryo (в секундах)
    # ОБНОВЛЕНО: 5 секунд для меньшей нагрузки
    POLL_INTERVAL = 5.0

    # Пауза после отправки сигнала (в секундах)
    # ОБНОВЛЕНО: 1 минута перерыва после алерта
    ALERT_COOLDOWN = 60.0


# --- Глобальное состояние ---
# Используем потокобезопасный подход для хранения последней цены Binance
class MarketData:
    def __init__(self):
        self._lock = threading.Lock()
        self._binance_ask = None

    def update_binance(self, price_str):
        with self._lock:
            try:
                self._binance_ask = Decimal(price_str)
            except Exception as e:
                logger.error(f"Ошибка конвертации цены Binance: {e}")

    def get_binance(self):
        with self._lock:
            return self._binance_ask


market_data = MarketData()


# --- Модуль Telegram ---
def send_telegram_alert(message):
    if not Config.TG_TOKEN or not Config.TG_CHAT_ID:
        logger.warning("Telegram токен или Chat ID не заданы. Алерт пропущен.")
        return False

    url = f"https://api.telegram.org/bot{Config.TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": Config.TG_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"  # Позволяет использовать жирный шрифт и моноширинный текст
    }

    try:
        response = requests.post(url, json=payload, timeout=5)
        if response.status_code != 200:
            logger.error(f"Ошибка отправки в TG: {response.text}")
            return False
        else:
            logger.info("Сигнал успешно отправлен в Telegram.")
            return True
    except Exception as e:
        logger.error(f"Сбой соединения с Telegram: {e}")
        return False


# --- Модуль Binance WebSocket ---
def on_message(ws, message):
    try:
        data = json.loads(message)
        # Извлекаем поле 'a' - Best Ask Price
        best_ask = data.get('a')
        if best_ask:
            market_data.update_binance(best_ask)
    except Exception as e:
        logger.error(f"Ошибка парсинга WS сообщения: {e}")


def on_error(ws, error):
    logger.error(f"WebSocket Ошибка: {error}")


def on_close(ws, close_status_code, close_msg):
    logger.warning("WebSocket соединение закрыто. Переподключение...")


def on_open(ws):
    logger.info("Подключено к Binance WebSocket (btcusdt@ticker)")


def run_binance_ws():
    # Настройка проверки сертификатов через certifi
    sslopt = {
        "cert_reqs": ssl.CERT_REQUIRED,
        "ca_certs": certifi.where(),
    }
    # Запускаем бесконечный цикл для авто-реконнекта при разрыве
    while True:
        try:
            ws = websocket.WebSocketApp(
                Config.BINANCE_WS,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )
            ws.run_forever(sslopt=sslopt)
        except Exception as e:
            logger.error(f"Критическая ошибка WS: {e}. Ждем 5 сек...")
            time.sleep(5)


# --- Модуль Mercuryo (REST) ---
def get_mercuryo_rate():
    # Важно: Mercuryo может блокировать запросы без User-Agent (ошибка 403)
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(Config.MERCURYO_API, params=Config.MERCURYO_PARAMS, headers=headers, timeout=5)

        if response.status_code == 200:
            data = response.json()
            # Пытаемся найти 'rate'. Структура может меняться, ищем на верхнем уровне или в data
            rate = data.get('rate')
            if not rate and 'data' in data:
                rate = data['data'].get('rate')

            if rate:
                return Decimal(str(rate))
            else:
                logger.warning(f"Поле 'rate' не найдено в ответе: {data}")
                return None
        elif response.status_code == 429:
            logger.warning("Mercuryo Rate Limit! Пауза 5 min.")
            time.sleep(300)
            return None
        else:
            logger.error(f"Mercuryo API Error {response.status_code}: {response.text}")
            return None
    except Exception as e:
        logger.error(f"Ошибка запроса к Mercuryo: {e}")
        return None


# --- Основной цикл (Main Loop) ---
def main():
    logger.info("Запуск скрипта мониторинга...")
    logger.info(f"Интервал опроса: {Config.POLL_INTERVAL} сек. Пауза после сигнала: {Config.ALERT_COOLDOWN} сек.")

    # 1. Запускаем WebSocket в отдельном потоке (Daemon thread)
    ws_thread = threading.Thread(target=run_binance_ws, daemon=True)
    ws_thread.start()

    # Даем пару секунд на подключение к Binance
    time.sleep(3)

    logger.info("Начинаем опрос Mercuryo и сравнение цен...")

    while True:
        # Получаем цены
        merc_rate = get_mercuryo_rate()
        bin_ask = market_data.get_binance()

        if merc_rate and bin_ask:
            # Считаем разницу
            # Формула: (Mercuryo - Binance)
            diff_abs = merc_rate - bin_ask

            # Считаем процент: (Diff / Binance) * 100
            diff_pct = (diff_abs / bin_ask) * 100

            log_msg = f"Binance: {bin_ask} | Mercuryo: {merc_rate} | Spread: {diff_pct:.4f}%"
            logger.info(log_msg)

            # Проверяем условия для сигнала
            # 1. Меньше 0.20% (слишком узкий спред или отрицательный)
            # 2. Больше 0.5% (слишком широкий спред)

            alert_triggered = False
            condition_desc = ""

            if diff_pct < Config.THRESHOLD_LOW:
                alert_triggered = True
                condition_desc = "📉 СПРЕД НИЖЕ 0.20%"
            elif diff_pct > Config.THRESHOLD_HIGH:
                alert_triggered = True
                condition_desc = "📈 СПРЕД ВЫШЕ 0.5%"

            if alert_triggered:
                # Формируем красивое сообщение для Telegram с явным указанием процента
                msg_text = (
                    f"🚨 **ALERT** 🚨\n\n"
                    f"{condition_desc}\n"
                    f"👉 **ТЕКУЩИЙ СПРЕД: {diff_pct:.4f}%** 👈\n\n"
                    f"🏦 **Mercuryo:** `{merc_rate}`\n"
                    f"🔶 **Binance Ask:** `{bin_ask}`\n"
                    f"💵 **Разница:** `{diff_abs:.2f} USD`"
                )
                sent_success = send_telegram_alert(msg_text)

                if sent_success:
                    # ОБНОВЛЕНО: Если сигнал отправлен, делаем длинную паузу
                    logger.info(f"Сигнал отправлен. Пауза {Config.ALERT_COOLDOWN} сек перед следующей проверкой...")
                    time.sleep(Config.ALERT_COOLDOWN)
                    # continue пропускает остаток цикла, чтобы не делать двойную задержку
                    continue

        else:
            if not bin_ask:
                logger.warning("Ждем данных от Binance WebSocket...")

        # Ждем перед следующим опросом (Rate Limit protection)
        time.sleep(Config.POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Остановка скрипта пользователем (Ctrl+C).")