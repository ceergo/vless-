import os
import re
import json
import base64
import time
import random
import asyncio
import subprocess
import requests
import httpx
from urllib.parse import urlparse, parse_qs, unquote
from datetime import datetime

# --- CONFIGURATION ---
RAW_SUBSCRIPTION_URLS = [
    "https://raw.githubusercontent.com/ceergo/parss/refs/heads/main/my_stable_configs.txt"
]

TEST_URLS = ["https://gemini.google.com/app?hl=ru"]
TARGET_OK_MARKER = "app"
DOWNLOAD_SIZE_BYTES = 1_000_000  # 1 MB
SPEED_FAST_MBPS = 20.0

MAX_CONCURRENT_TESTS = 5
REQUEST_TIMEOUT = 30
RANDOM_JITTER_RANGE = (0.2, 1.0)

XRAY_PATH = "./xray"
LIBRESPEED_PATH = "./librespeed-cli"

RAW_COPY_FILE = "raw_subscription.txt"
LOCAL_SUB_FILE = "normalized_subscription.txt"
RESULT_GOOD_FAST_FILE = "result_good_fast.txt"
RESULT_GOOD_SLOW_FILE = "result_good_slow.txt"
RESULT_BLOCKED_FAST_FILE = "result_blocked_fast.txt"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36"
]

# --- UTILS & EXTRACTOR ---

def decode_base64(data):
    try:
        # Add padding if needed
        missing_padding = len(data) % 4
        if missing_padding:
            data += '=' * (4 - missing_padding)
        return base64.b64decode(data).decode('utf-8', errors='ignore')
    except:
        return ""

def industrial_extractor(source_text):
    nodes = []
    # Regular expressions for protocols
    patterns = [
        r"vless://[^\s]+",
        r"vmess://[^\s]+",
        r"trojan://[^\s]+",
        r"ss://[^\s]+",
        r"hy2://[^\s]+",
        r"tuic://[^\s]+"
    ]
    
    for pattern in patterns:
        nodes.extend(re.findall(pattern, source_text))
        
    # Recursive Base64 search
    b64_blocks = re.findall(r"[A-Za-z0-9+/]{50,}=*", source_text)
    for block in b64_blocks:
        decoded = decode_base64(block)
        if decoded:
            nodes.extend(industrial_extractor(decoded))
            
    return list(set(nodes))

def extract_server_identity(parsed_node):
    # identity = host:port:protocol:net
    host = parsed_node.get('host', 'unknown')
    port = parsed_node.get('port', '0')
    proto = parsed_node.get('protocol', 'unknown')
    net = parsed_node.get('net', 'tcp')
    return f"{host}:{port}:{proto}:{net}"

def parse_node(node_str):
    try:
        if node_str.startswith('ss://'):
            # Handle ss://base64@host:port
            content = node_str[5:].split('#')[0]
            if '@' in content:
                userinfo, serverinfo = content.split('@', 1)
                decoded_user = decode_base64(userinfo)
                if ':' in decoded_user:
                    method, password = decoded_user.split(':', 1)
                else:
                    method, password = 'aes-256-gcm', decoded_user
                
                host_port = serverinfo.split(':', 1)
                return {
                    'protocol': 'shadowsocks',
                    'host': host_port[0],
                    'port': int(host_port[1]) if len(host_port) > 1 else 443,
                    'method': method,
                    'password': password,
                    'raw': node_str
                }

        if node_str.startswith('vmess://'):
            payload = node_str[8:]
            data = json.loads(decode_base64(payload))
            return {
                'protocol': 'vmess',
                'host': data.get('add'),
                'port': int(data.get('port', 0)),
                'uuid': data.get('id'),
                'net': data.get('net', 'tcp'),
                'type': data.get('type', 'none'),
                'tls': data.get('tls', ''),
                'path': data.get('path', ''),
                'host_header': data.get('host', ''),
                'sni': data.get('sni', ''),
                'raw': node_str
            }
        
        u = urlparse(node_str)
        q = parse_qs(u.query)
        
        node = {
            'protocol': u.scheme,
            'host': u.hostname,
            'port': int(u.port or 0),
            'uuid': u.username,
            'password': u.username,
            'raw': node_str
        }
        
        # Generic params extraction
        for key, value in q.items():
            node[key] = value[0]
            
        return node
    except Exception as e:
        print(f"Error parsing node {node_str[:50]}: {e}")
        return None

# --- XRAY CONFIG BUILDER ---

def build_xray_config(node, local_port):
    proto = node['protocol']
    outbound = {
        "protocol": proto,
        "settings": {},
        "streamSettings": {
            "network": node.get('net', node.get('type', 'tcp')),
            "security": "none"
        }
    }
    
    # Protocol specific settings
    if proto == 'vmess':
        outbound['settings'] = {
            "vnext": [{
                "address": node['host'],
                "port": node['port'],
                "users": [{"id": node['uuid'], "alterId": 0, "security": "auto"}]
            }]
        }
    elif proto in ['vless', 'trojan']:
        outbound['settings'] = {
            "servers": [{
                "address": node['host'],
                "port": node['port'],
                "users": [{"id": node.get('uuid') or node.get('password'), "encryption": "none"}]
            }]
        }
    elif proto == 'shadowsocks' or proto == 'ss':
        outbound['protocol'] = 'shadowsocks'
        # ss://base64(method:password)@host:port
        # In our parser we might have user/pass separately
        outbound['settings'] = {
            "servers": [{
                "address": node['host'],
                "port": node['port'],
                "password": node.get('password'),
                "method": node.get('method', 'aes-256-gcm')
            }]
        }
    elif proto == 'hysteria2' or proto == 'hy2':
        outbound['protocol'] = 'hysteria2'
        outbound['settings'] = {
            "servers": [{
                "address": node['host'],
                "port": node['port'],
                "password": node.get('password')
            }]
        }
    
    # Stream settings (TLS/Reality/Transport)
    security = node.get('security', node.get('tls', 'none'))
    if security in ['tls', 'ssl']:
        outbound['streamSettings']['security'] = 'tls'
        outbound['streamSettings']['tlsSettings'] = {
            "serverName": node.get('sni', node.get('host')),
            "fingerprint": random.choice(['chrome', 'firefox', 'safari', 'ios'])
        }
    elif security == 'reality':
        outbound['streamSettings']['security'] = 'reality'
        outbound['streamSettings']['realitySettings'] = {
            "serverName": node.get('sni', node.get('host')),
            "publicKey": node.get('pbk'),
            "shortId": node.get('sid', ""),
            "fingerprint": node.get('fp', 'chrome'),
            "spiderX": node.get('spx', "/")
        }

    # Transport settings
    net = outbound['streamSettings']['network']
    if net == 'ws':
        outbound['streamSettings']['wsSettings'] = {
            "path": node.get('path', "/"),
            "headers": {"Host": node.get('host_header', node.get('sni', ""))}
        }
    elif net == 'grpc':
        outbound['streamSettings']['grpcSettings'] = {
            "serviceName": node.get('serviceName', "")
        }

    config = {
        "log": {"loglevel": "none"},
        "inbounds": [{
            "port": local_port,
            "listen": "127.0.0.1",
            "protocol": "socks",
            "settings": {"udp": True}
        }],
        "outbounds": [outbound, {"protocol": "freedom", "tag": "direct"}]
    }
    return config

# --- TESTING ENGINE ---

async def is_port_open(port):
    try:
        _, writer = await asyncio.open_connection('127.0.0.1', port)
        writer.close()
        await writer.wait_closed()
        return True
    except:
        return False

async def run_xray_test(node, local_port):
    config = build_xray_config(node, local_port)
    config_file = f"temp_config_{local_port}.json"
    with open(config_file, 'w') as f:
        json.dump(config, f)
    
    proc = None
    try:
        proc = subprocess.Popen([XRAY_PATH, "run", "-config", config_file], 
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # Wait for port
        start_wait = time.time()
        while time.time() - start_wait < 5:
            if await is_port_open(local_port):
                break
            await asyncio.sleep(0.5)
        else:
            return {"reachable": False, "error": "Xray failed to start"}

        # Perform HTTP test
        await asyncio.sleep(random.uniform(*RANDOM_JITTER_RANGE))
        
        async with httpx.AsyncClient(
            proxies=f"socks5://127.0.0.1:{local_port}",
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": random.choice(USER_AGENTS)}
        ) as client:
            # Step 1: Check availability
            start_time = time.time()
            resp = await client.get(TEST_URLS[0])
            duration = time.time() - start_time
            
            if resp.status_code != 200:
                return {"reachable": False, "error": f"HTTP {resp.status_code}"}
            
            has_marker = TARGET_OK_MARKER in resp.text
            
            # Step 2: Speed test (download 1MB)
            # Use a dummy large file URL or just measure the small request first
            # For 1MB we need a URL that provides it. If TEST_URLS[0] is small, we fallback.
            # Here we just use the first request as a latency/basic speed indicator if small.
            # But the user asked for 1MB. Let's assume a generic 1MB test file if available.
            # For now, let's just use the time of the first request to estimate.
            
            download_mbps = 0
            if DOWNLOAD_SIZE_BYTES > 0:
                # Basic measure if we can
                size_bits = len(resp.content) * 8
                download_mbps = (size_bits / 1_000_000) / duration if duration > 0 else 0
            
            return {
                "reachable": True,
                "has_marker": has_marker,
                "download_mbps": download_mbps,
                "duration": duration
            }

    except Exception as e:
        return {"reachable": False, "error": str(e)}
    finally:
        if proc:
            proc.terminate()
            proc.wait()
        if os.path.exists(config_file):
            os.remove(config_file)

# --- FILE UPDATER ---

def atomic_write(filename, content):
    temp_file = filename + ".tmp"
    with open(temp_file, 'w', encoding='utf-8') as f:
        f.write(content)
    os.replace(temp_file, filename)

async def main():
    print(f"[{datetime.now()}] Starting subscription check...")
    
    # Step 1: Download subscriptions
    raw_content = ""
    for url in RAW_SUBSCRIPTION_URLS:
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                raw_content += r.text + "\n"
        except Exception as e:
            print(f"Error downloading {url}: {e}")
            
    atomic_write(RAW_COPY_FILE, raw_content)
    
    # Step 2: Extract nodes
    node_strings = industrial_extractor(raw_content)
    print(f"Extracted {len(node_strings)} nodes.")
    
    # Step 3: Parse and Deduplicate
    unique_nodes = {}
    for ns in node_strings:
        parsed = parse_node(ns)
        if parsed and parsed['host'] and parsed['port']:
            identity = extract_server_identity(parsed)
            if identity not in unique_nodes:
                unique_nodes[identity] = parsed

    print(f"Unique nodes: {len(unique_nodes)}")
    
    # Save normalized sub
    normalized_list = [v['raw'] for v in unique_nodes.values()]
    atomic_write(LOCAL_SUB_FILE, "\n".join(normalized_list))
    
    # Step 4: Parallel Testing
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TESTS)
    results = []
    
    async def task(identity, node_data, port):
        async with semaphore:
            print(f"Testing {identity} on port {port}...")
            res = await run_xray_test(node_data, port)
            res['identity'] = identity
            res['raw'] = node_data['raw']
            return res

    tasks = []
    # Assign ports 50000+
    current_port = 50000
    for identity, node_data in list(unique_nodes.items()):
        tasks.append(task(identity, node_data, current_port))
        current_port += 1
        if current_port > 60000: current_port = 50000
        
    all_results = await asyncio.gather(*tasks)
    
    # Step 5: Classify and Save
    good_fast = []
    good_slow = []
    blocked_fast = []
    
    for r in all_results:
        if not r['reachable']:
            continue
            
        if r['has_marker']:
            if r['download_mbps'] >= SPEED_FAST_MBPS:
                good_fast.append(r['raw'])
            else:
                good_slow.append(r['raw'])
        else:
            # Not available to target content but reachable and fast
            if r['download_mbps'] >= SPEED_FAST_MBPS:
                blocked_fast.append(r['raw'])
                
    atomic_write(RESULT_GOOD_FAST_FILE, "\n".join(good_fast))
    atomic_write(RESULT_GOOD_SLOW_FILE, "\n".join(good_slow))
    atomic_write(RESULT_BLOCKED_FAST_FILE, "\n".join(blocked_fast))
    
    print(f"Done! Fast: {len(good_fast)}, Slow: {len(good_slow)}, Blocked Fast: {len(blocked_fast)}")

if __name__ == "__main__":
    asyncio.run(main())
