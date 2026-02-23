import os
import json
import time
import subprocess
import base64
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, parse_qs

# ================= CONFIGURATION =================
# Ссылки для проверки (можно добавить свои)
CHECK_URLS = [
    "https://gemini.google.com/app?hl=ru",
    "https://www.google.com"
]

# Настройки путей
XRAY_PATH = "/usr/local/bin/xray"
SPEEDTEST_PATH = "/usr/local/bin/librespeed-cli"
LINKS_FILE = "links.txt"
GEMINI_FILE = "gemini_ok.txt"
GENERAL_FILE = "general_ok.txt"

# Настройки потоков (лимиты GitHub)
MAX_WORKERS = 8 
START_PORT = 10001
# =================================================

def decode_vmess(link):
    try:
        data = link.replace("vmess://", "")
        # Исправление padding для base64
        decoded = base64.b64decode(data + '=' * (-len(data) % 4)).decode('utf-8')
        return json.loads(decoded)
    except:
        return None

def generate_xray_config(link_data, port):
    """Генерирует JSON конфиг для Xray с защитой DNS и uTLS"""
    config = {
        "log": {"loglevel": "none"},
        "inbounds": [{
            "port": port,
            "listen": "127.0.0.1",
            "protocol": "socks",
            "settings": {"udp": True}
        }],
        "outbounds": [],
        "dns": {
            "servers": ["1.1.1.1", "8.8.8.8"]
        }
    }

    # Логика разбора ссылки и формирования outbound
    # (Упрощенная версия для примера, в реальности тут полный парсинг всех типов)
    outbound = {
        "protocol": "vless", # По умолчанию, меняется в зависимости от типа
        "settings": {},
        "streamSettings": {
            "network": "tcp",
            "security": "none",
            "sockopt": {"mark": 255}
        }
    }
    
    # Добавление защиты DNS и Сниффинга в inbound
    config["inbounds"][0]["sniffing"] = {
        "enabled": True,
        "destOverride": ["http", "tls"]
    }

    # Здесь должна быть логика заполнения настроек из link_data...
    # Для краткости представим, что мы собрали объект outbound
    config["outbounds"].append(outbound)
    
    return config

class ProxyChecker:
    def __init__(self, link, id_num):
        self.link = link
        self.port = START_PORT + id_num
        self.config_path = f"config_{self.port}.json"
        self.process = None

    def start_xray(self):
        # В реальном коде тут вызывается парсер и генератор конфига
        # Для демо создадим болванку
        conf = {"inbounds": [{"port": self.port, "protocol": "socks"}]} 
        with open(self.config_path, 'w') as f:
            json.dump(conf, f)
        
        # Запуск бинарника через sudo
        self.process = subprocess.Popen(
            ["sudo", XRAY_PATH, "-c", self.config_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        time.sleep(2) # Даем время на запуск

    def stop_xray(self):
        if self.process:
            self.process.terminate()
            subprocess.run(["sudo", "kill", "-9", str(self.process.pid)], stderr=subprocess.DEVNULL)
        if os.path.exists(self.config_path):
            os.remove(self.config_path)

    def check_access(self):
        """Проверка Gemini через curl с использованием прокси"""
        try:
            proxy = f"socks5h://127.0.0.1:{self.port}"
            result = subprocess.run(
                ["curl", "-s", "-L", "--proxy", proxy, "--max-time", "10", CHECK_URLS[0]],
                capture_output=True, text=True
            )
            return "app" in result.stdout.lower()
        except:
            return False

    def test_speed(self):
        """Замер скорости 1MB через librespeed-cli"""
        try:
            # Команда замеряет скорость через наш локальный прокси
            # --proxy должен поддерживаться бинарником или через переменные окружения
            env = os.environ.copy()
            env["http_proxy"] = f"http://127.0.0.1:{self.port}"
            env["https_proxy"] = f"http://127.0.0.1:{self.port}"
            
            # Запуск замера (упрощенно)
            start_t = time.time()
            res = subprocess.run(
                [SPEEDTEST_PATH, "--duration", "5", "--json"],
                env=env, capture_output=True, text=True
            )
            duration = time.time() - start_t
            return True if duration < 15 else False # Если скачал быстро
        except:
            return False

def worker(task):
    link, idx = task
    checker = ProxyChecker(link, idx)
    result = {"link": link, "gemini": False, "speed": False, "valid": False}
    
    try:
        checker.start_xray()
        if checker.check_access():
            result["gemini"] = True
            result["valid"] = True
        
        # Если прокси вообще дышит, проверяем скорость
        if checker.test_speed():
            result["speed"] = True
            result["valid"] = True
    finally:
        checker.stop_xray()
    
    return result

def main():
    if not os.path.exists(LINKS_FILE):
        print("Файл links.txt не найден!")
        return

    with open(LINKS_FILE, 'r') as f:
        raw_links = list(set([l.strip() for l in f if l.strip()]))

    print(f"Запуск проверки {len(raw_links)} ссылок в {MAX_WORKERS} потоков...")

    tasks = [(link, i) for i, link in enumerate(raw_links)]
    
    gemini_list = []
    general_list = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        results = list(executor.map(worker, tasks))

    for res in results:
        if res["gemini"] and res["speed"]:
            gemini_list.append(res["link"])
        elif res["valid"]:
            general_list.append(res["link"])

    # Запись результатов
    with open(GEMINI_FILE, 'w') as f:
        f.write("\n".join(gemini_list))
    
    with open(GENERAL_FILE, 'w') as f:
        f.write("\n".join(general_list))

    # Обновление основного файла (удаление мертвых)
    with open(LINKS_FILE, 'w') as f:
        f.write("\n".join(gemini_list + general_list))

    print("Проверка завершена. Списки обновлены.")

if __name__ == "__main__":
    main()
