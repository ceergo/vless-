import os
import json
import time
import subprocess
import base64
import re
import signal
import requests
from concurrent.futures import ThreadPoolExecutor

# --- CONFIGURATION ---
# Внешний источник стабильных конфигов
REMOTE_STABLE_URL = "https://raw.githubusercontent.com/ceergo/parss/main/my_stable_configs.txt"

TARGET_URL = "https://gemini.google.com/app?hl=ru"
CHECK_MARKER = "app"
XRAY_BIN = "/usr/local/bin/xray"

INPUT_FILE = "links.txt"
RESULT_GEMINI = "gemini_ok.txt"
RESULT_GENERAL = "general_ok.txt"

MAX_THREADS = 12
START_PORT = 15000

def parse_link(link):
    """Parses proxy link type and prepares for config generation"""
    link = link.strip()
    if not link: return None
    proto = link.split("://")[0]
    return {"protocol": proto, "raw": link}

def generate_config(link_data, port):
    """Generates Xray JSON config with uTLS and DNS leak protection"""
    config = {
        "log": {"loglevel": "none"},
        "dns": {
            "servers": ["1.1.1.1", "8.8.8.8", "localhost"],
            "queryStrategy": "UseIPv4"
        },
        "inbounds": [{
            "port": port,
            "listen": "127.0.0.1",
            "protocol": "socks",
            "settings": {"udp": True},
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]}
        }],
        "outbounds": [
            {
                "protocol": "freedom", 
                "settings": {},
                "streamSettings": {
                    "sockopt": {"mark": 255},
                    "tlsSettings": {"fingerprint": "chrome"}
                }
            }
        ]
    }
    return config

class ProxyChecker:
    def __init__(self, link, idx):
        self.link = link
        self.port = START_PORT + idx
        self.config_path = f"config_{self.port}.json"
        self.proc = None

    def start(self):
        link_data = parse_link(self.link)
        if not link_data: return False
        
        cfg = generate_config(link_data, self.port)
        with open(self.config_path, "w") as f:
            json.dump(cfg, f)
        
        # Start Xray core via sudo for root-level networking
        self.proc = subprocess.Popen(
            ["sudo", XRAY_BIN, "-c", self.config_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid
        )
        time.sleep(2.5) # Wait for core initialization
        return True

    def test(self):
        res = {"gemini": False, "speed": 0, "active": False}
        proxy = f"socks5h://127.0.0.1:{self.port}"
        
        try:
            # Step 1: Gemini Access Test
            check = subprocess.run(
                ["curl", "-sL", "--proxy", proxy, "--max-time", "12", TARGET_URL],
                capture_output=True, text=True
            )
            if CHECK_MARKER in check.stdout.lower():
                res["gemini"] = True
                res["active"] = True
            elif check.returncode == 0 and len(check.stdout) > 50:
                res["active"] = True

            # Step 2: Speed Test (1MB download)
            if res["active"]:
                speed_check = subprocess.run(
                    ["curl", "-s", "--proxy", proxy, "-o", "/dev/null", "-w", "%{speed_download}", 
                     "--max-time", "15", "https://speed.cloudflare.com/__down?bytes=1048576"],
                    capture_output=True, text=True
                )
                try:
                    res["speed"] = float(speed_check.stdout.strip()) / 1024 / 1024 # MB/s
                except:
                    res["speed"] = 0
        except:
            pass
        return res

    def stop(self):
        if self.proc:
            try:
                # Terminate entire process group (sudo + xray)
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except:
                pass
        if os.path.exists(self.config_path):
            os.remove(self.config_path)

def process_task(data):
    link, i = data
    chk = ProxyChecker(link, i)
    status = {"link": link, "type": "dead"}
    
    try:
        if chk.start():
            metrics = chk.test()
            if metrics["gemini"] and metrics["speed"] > 0.1:
                status["type"] = "gemini"
            elif metrics["active"] and metrics["speed"] > 0.05:
                status["type"] = "general"
    finally:
        chk.stop()
    
    return status

def main():
    # 1. Fetch remote stable configs
    print(f"Fetching remote configs from {REMOTE_STABLE_URL}...")
    remote_links = []
    try:
        r = requests.get(REMOTE_STABLE_URL, timeout=15)
        if r.status_code == 200:
            remote_links = [l.strip() for l in r.text.splitlines() if l.strip()]
            print(f"Successfully fetched {len(remote_links)} remote links.")
    except Exception as e:
        print(f"Warning: Failed to fetch remote file: {e}")

    # 2. Read local links
    local_links = []
    if os.path.exists(INPUT_FILE):
        with open(INPUT_FILE, "r") as f:
            local_links = [line.strip() for line in f if line.strip()]
        print(f"Read {len(local_links)} local links.")

    # Merge sources and deduplicate
    all_links = list(set(local_links + remote_links))
    print(f"Total unique links to check: {len(all_links)}")

    if not all_links:
        print("No links found to process.")
        return

    # 3. Parallel Processing
    with ThreadPoolExecutor(max_workers=MAX_THREADS) as pool:
        results = list(pool.map(process_task, [(l, i) for i, l in enumerate(all_links)]))

    # Sort results into categories
    gemini_ok = [r["link"] for r in results if r["type"] == "gemini"]
    general_ok = [r["link"] for r in results if r["type"] == "general"]

    # 4. Save results
    with open(RESULT_GEMINI, "w") as f: 
        f.write("\n".join(sorted(set(gemini_ok))))
    
    with open(RESULT_GENERAL, "w") as f: 
        f.write("\n".join(sorted(set(general_ok))))
    
    # Update main links.txt with only working configurations
    with open(INPUT_FILE, "w") as f: 
        f.write("\n".join(sorted(set(gemini_ok + general_ok))))

    print(f"--- Processing Complete ---")
    print(f"Gemini-Ready (High Speed): {len(gemini_ok)}")
    print(f"General-Working: {len(general_ok)}")
    print(f"Dead/Filtered: {len(all_links) - len(gemini_ok) - len(general_ok)}")

if __name__ == "__main__":
    main()
