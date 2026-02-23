import os, json, time, subprocess, base64, signal, requests, re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, parse_qs, unquote

REMOTE_STABLE_URL = "vmess://ewogICAgImFkZCI6ICIxODYuMTkwLjIxNS43NSIsCiAgICAiYWlkIjogMSwKICAgICJob3N0IjogIjE4Ni4xOTAuMjE1Ljc1IiwKICAgICJpZCI6ICIwM2ZjYzYxOC1iOTNkLTY3OTYtNmFlZC04YTM4Yzk3NWQ1ODEiLAogICAgIm5ldCI6ICJ3cyIsCiAgICAicGF0aCI6ICJsaW5rdndzIiwKICAgICJwb3J0IjogNDQzLAogICAgInBzIjogIvCfh6jwn4etQ0gtMTg2LjE5MC4yMTUuNzUtNzgwOSIsCiAgICAidGxzIjogInRscyIsCiAgICAidHlwZSI6ICJhdXRvIiwKICAgICJzZWN1cml0eSI6ICJhdXRvIiwKICAgICJza2lwLWNlcnQtdmVyaWZ5IjogdHJ1ZSwKICAgICJzbmkiOiAiIgp9"
TARGET_URL = "https://gemini.google.com/app?hl=ru"
CHECK_MARKER = "app"
XRAY_BIN = "/usr/local/bin/xray"
INPUT_FILE = "links.txt"
MAX_THREADS = 15
START_PORT = 15000

def decode_base64(data):
    data = data.strip()
    try:
        missing_padding = len(data) % 4
        if missing_padding: data += '=' * (4 - missing_padding)
        return base64.b64decode(data).decode('utf-8')
    except: return data

def extract_links(content):
    decoded = decode_base64(content)
    # Ищем всё, что похоже на прокси-ссылки
    found = re.findall(r'(vless|vmess|trojan|ss|ssr)://[^\s<>"]+', decoded)
    return list(set(found)) if found else [l for l in decoded.splitlines() if "://" in l]

def parse_vless(link):
    parsed = urlparse(link)
    params = parse_qs(parsed.query)
    return {
        "protocol": "vless",
        "settings": {"vnext": [{"address": parsed.hostname, "port": parsed.port, "users": [{"id": parsed.username, "encryption": "none"}]}]},
        "streamSettings": {
            "network": params.get("type", ["tcp"])[0],
            "security": params.get("security", ["none"])[0],
            "tlsSettings": {"serverName": params.get("sni", [""])[0], "fingerprint": params.get("fp", ["chrome"])[0]} if params.get("security") == ["tls"] else {},
            "realitySettings": {"serverName": params.get("sni", [""])[0], "fingerprint": params.get("fp", ["chrome"])[0], "publicKey": params.get("pbk", [""])[0], "shortId": params.get("sid", [""])[0], "spiderX": params.get("spx", ["/"])[0]} if params.get("security") == ["reality"] else {}
        }
    }

def parse_vmess(link):
    try:
        data = json.loads(decode_base64(link[8:]))
        return {
            "protocol": "vmess",
            "settings": {"vnext": [{"address": data["add"], "port": int(data["port"]), "users": [{"id": data["id"], "alterId": int(data["aid"]), "security": "auto"}]}]},
            "streamSettings": {"network": data.get("net", "tcp"), "security": "tls" if data.get("tls") == "tls" else "none", "tlsSettings": {"serverName": data.get("sni", "")}}
        }
    except: return None

class ProxyChecker:
    def __init__(self, link, idx):
        self.link = link
        self.port = START_PORT + idx
        self.config_path = f"config_{self.port}.json"
        self.proc = None

    def start(self):
        outbound = None
        if self.link.startswith("vless://"): outbound = parse_vless(self.link)
        elif self.link.startswith("vmess://"): outbound = parse_vmess(self.link)
        
        if not outbound: return False
        
        config = {
            "log": {"loglevel": "none"},
            "inbounds": [{"port": self.port, "listen": "127.0.0.1", "protocol": "socks", "sniffing": {"enabled": True, "destOverride": ["http", "tls"]}}],
            "outbounds": [outbound, {"protocol": "freedom", "tag": "direct"}]
        }
        with open(self.config_path, "w") as f: json.dump(config, f)
        self.proc = subprocess.Popen(["sudo", XRAY_BIN, "-c", self.config_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
        time.sleep(3)
        return True

    def test(self):
        res = {"gemini": False, "active": False, "speed": 0}
        proxy = f"socks5h://127.0.0.1:{self.port}"
        try:
            c = subprocess.run(["curl", "-sL", "--proxy", proxy, "--max-time", "12", TARGET_URL], capture_output=True, text=True)
            if CHECK_MARKER in c.stdout.lower(): res["gemini"] = res["active"] = True
            elif c.returncode == 0: res["active"] = True
            
            if res["active"]:
                s = subprocess.run(["curl", "-s", "--proxy", proxy, "-o", "/dev/null", "-w", "%{speed_download}", "--max-time", "15", "https://speed.cloudflare.com/__down?bytes=1048576"], capture_output=True, text=True)
                res["speed"] = float(s.stdout.strip()) / 1024 / 1024
        except: pass
        return res

    def stop(self):
        if self.proc:
            try: os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except: pass
        if os.path.exists(self.config_path): os.remove(self.config_path)

def process_task(data):
    link, i = data
    chk = ProxyChecker(link, i)
    status = {"link": link, "type": "dead", "speed": 0}
    try:
        if chk.start():
            m = chk.test()
            status["speed"] = m["speed"]
            if m["gemini"] and m["speed"] > 0.1: status["type"] = "gemini"
            elif m["active"]: status["type"] = "general"
    finally: chk.stop()
    return status

def main():
    all_content = ""
    try:
        r = requests.get(REMOTE_STABLE_URL, timeout=15)
        if r.status_code == 200: all_content += r.text + "\n"
    except: pass
    if os.path.exists(INPUT_FILE):
        with open(INPUT_FILE, "r") as f: all_content += f.read()
    
    links = extract_links(all_content)
    print(f"Total links found after decoding: {len(links)}")
    
    with ThreadPoolExecutor(max_workers=MAX_THREADS) as pool:
        results = list(pool.map(process_task, [(l, i) for i, l in enumerate(links)]))
    
    g_ok = sorted([r["link"] for r in results if r["type"] == "gemini"], key=lambda x: next((res["speed"] for res in results if res["link"] == x), 0), reverse=True)
    o_ok = [r["link"] for r in results if r["type"] == "general"]
    
    with open("gemini_ok.txt", "w") as f: f.write("\n".join(g_ok))
    with open("general_ok.txt", "w") as f: f.write("\n".join(o_ok))
    with open(INPUT_FILE, "w") as f: f.write("\n".join(set(g_ok + o_ok)))
    print(f"Done! Gemini: {len(g_ok)}, General: {len(o_ok)}")

if __name__ == "__main__":
    main()
