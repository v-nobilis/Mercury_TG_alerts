import ssl
import certifi
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

# --- НОВЫЕ ИМПОРТЫ ДЛЯ GRAFANA/INFLUXDB ---
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# --- Настройка окружения ---
env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_path):
    load_dotenv(env_path)
else:
    print("ВНИМАНИЕ: Файл.env не найден!")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("ArbMonitor")
getcontext().prec = 8


# --- КОНФИГУРАЦИЯ ---
class Config:
    BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@ticker"
    MERCURYO_API = "https://api.mercuryo.io/v1.6/public/convert"
    MERCURYO_PARAMS = {"from": "USD", "to": "BTC", "amount": "100", "type": "buy"}

    TG_TOKEN = os.getenv("TG_TOKEN")
    TG_CHAT_ID = os.getenv("TG_CHAT_ID")

    THRESHOLD_LOW = Decimal("0.1")
    THRESHOLD_HIGH = Decimal("0.25")

    # ОБНОВЛЕНО: Интервал 10 секунд
    POLL_INTERVAL = 10.0

    # Кулдаун для Телеграма
    ALERT_COOLDOWN = 60.0

    # --- НАСТРОЙКИ GRAFANA / INFLUXDB ---
    INFLUX_URL = "https://eu-central-1-1.aws.cloud2.influxdata.com/"
    INFLUX_TOKEN = "gfH4qW_kDybtTbnQY7gFxvgeLC31dWYZ8hLte0VUjNmhDs8hDgvN3hI8yHABFqGMAIwTBcTF0wvQ4bFM1Cp0IQ=="
    INFLUX_ORG = "d5619b358e0982ea"
    INFLUX_BUCKET = "monitor_data"


# --- СОСТОЯНИЕ РЫНКА ---
class MarketData:
    def __init__(self):
        self._lock = threading.Lock()
        self._binance_ask = None

    def update_binance(self, price_str):
        with self._lock:
            try:
                self._binance_ask = Decimal(price_str)
            except Exception as e:
                logger.error(f"Err convert: {e}")

    def get_binance(self):
        with self._lock:
            return self._binance_ask


market_data = MarketData()

try:
    influx_client = InfluxDBClient(
        url=Config.INFLUX_URL,
        token=Config.INFLUX_TOKEN,
        org=Config.INFLUX_ORG,
        verify_ssl=True,
        ssl_ca_cert = certifi.where(),
    )
    write_api = influx_client.write_api(write_options=SYNCHRONOUS)
    logger.info("InfluxDB клиент инициализирован (SSL verification disabled)")
except Exception as e:
    logger.error(f"Ошибка инициализации InfluxDB: {e}")
    sys.exit(1)


# --- TELEGRAM ---
def send_telegram_alert(message):
    if not Config.TG_TOKEN or not Config.TG_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{Config.TG_TOKEN}/sendMessage"
    payload = {"chat_id": Config.TG_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
        return True
    except Exception as e:
        logger.error(f"TG Error: {e}")
        return False


# --- BINANCE WS ---
def on_message(ws, message):
    try:
        data = json.loads(message)
        best_ask = data.get('a')
        if best_ask:
            market_data.update_binance(best_ask)
    except:
        pass


def run_binance_ws():
    sslopt = {"cert_reqs": ssl.CERT_NONE}  # Тоже отключаем строгую проверку для WS
    while True:
        try:
            ws = websocket.WebSocketApp(
                Config.BINANCE_WS,
                on_message=on_message,
                on_error=lambda ws, err: logger.error(f"WS Err: {err}"),
                on_close=lambda ws, *args: logger.warning("WS Closed")
            )
            ws.run_forever(sslopt=sslopt)
        except Exception as e:
            logger.error(f"WS Critical: {e}")
            time.sleep(5)


# --- MERCURYO ---
def get_mercuryo_rate():
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(Config.MERCURYO_API, params=Config.MERCURYO_PARAMS, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            rate = data.get('rate') or data.get('data', {}).get('rate')
            return Decimal(str(rate)) if rate else None
        elif response.status_code == 429:
            logger.warning("Mercuryo Rate Limit!")
            return None
    except Exception:
        return None


# --- MAIN ---
def main():
    logger.info("Запуск... Интервал: 10 сек. Графики пишутся всегда.")

    threading.Thread(target=run_binance_ws, daemon=True).start()
    time.sleep(3)

    last_alert_time = 0  # Таймер для кулдауна ТГ

    while True:
        merc_rate = get_mercuryo_rate()
        bin_ask = market_data.get_binance()

        if merc_rate and bin_ask:
            diff_abs = merc_rate - bin_ask
            diff_pct = (diff_abs / bin_ask) * 100

            # Логируем в консоль
            logger.info(f"Binance: {bin_ask} | Mercuryo: {merc_rate} | Spread: {diff_pct:.4f}%")

            # 1. ОТПРАВКА В GRAFANA (ВСЕГДА)
            try:

                p = Point("spread_monitor") \
                    .tag("pair", "BTC/USDT") \
                    .field("spread_pct", float(diff_pct)) \
                    .field("binance", float(bin_ask)) \
                    .field("mercuryo", float(merc_rate))

                write_api.write(bucket=Config.INFLUX_BUCKET, org=Config.INFLUX_ORG, record=p)
            except Exception as e:
                logger.error(f"Ошибка записи в Grafana: {e}")

            # 2. ПРОВЕРКА АЛЕРТОВ (Только если прошел кулдаун)
            if diff_pct < Config.THRESHOLD_LOW or diff_pct > Config.THRESHOLD_HIGH:
                current_time = time.time()

                # Если прошло больше 60 сек с последнего алерта
                if (current_time - last_alert_time) > Config.ALERT_COOLDOWN:

                    desc = "📉 НИЖЕ 0.1%" if diff_pct < Config.THRESHOLD_LOW else "📈 ВЫШЕ 0.25%"
                    msg = (f"🚨 **ALERT** {desc}\n"
                           f"Spread: **{diff_pct:.4f}%**\n"
                           f"Merc: `{merc_rate}` | Bin: `{bin_ask}`")

                    if send_telegram_alert(msg):
                        logger.info(">>> Алерт отправлен в Telegram")
                        last_alert_time = current_time  # Обновляем таймер
                else:
                    logger.info("(Алерт пропущен - действует кулдаун)")

        else:
            if not bin_ask: logger.warning("Ждем цену Binance...")

        # Ждем 10 секунд и повторяем (независимо от алертов)
        time.sleep(Config.POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Стоп.")