import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import re
import tempfile
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import httpx
import requests


# ==========================
# Конфигурация (удобно менять)
# ==========================

# Список RAW-подписок (можно расширять/редактировать)
RAW_SUBSCRIPTION_URLS: List[str] = [
    "https://raw.githubusercontent.com/ceergo/parss/refs/heads/main/my_stable_configs.txt",
]

# Целевые URL для проверки доступности (берём первый)
TEST_URLS: List[str] = [
    "https://gemini.google.com/app?hl=ru",
]

# Маркер "успешной" страницы
TARGET_OK_MARKER: str = "app"

# Параметры теста скорости
DOWNLOAD_SIZE_BYTES: int = 1_000_000  # 1 МБ
SPEED_FAST_MBPS: float = 20.0

# Ограничение конкурентности и тайм-ауты
MAX_CONCURRENT_TESTS: int = 3
REQUEST_TIMEOUT: float = 20.0
XRAY_START_TIMEOUT: float = 10.0
NODE_OVERALL_TIMEOUT: float = 60.0  # жёсткий тайм-аут на один узел

# Рандомизация задержек и заголовков
RANDOM_JITTER_RANGE: Tuple[float, float] = (0.2, 1.0)
USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]
ACCEPT_LANGUAGES: List[str] = [
    "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "ru,en-US;q=0.8,en;q=0.7",
]

TLS_FINGERPRINTS: List[str] = [
    "chrome",
    "firefox",
    "ios",
    "android",
]

# Пути к бинарникам (скачиваются в workflow)
XRAY_PATH: str = "./xray"
LIBRESPEED_PATH: str = "./librespeed-cli"

# Имена файлов
RAW_COPY_FILE: str = "raw_subscription.txt"
LOCAL_SUB_FILE: str = "normalized_subscription.txt"
RESULT_GOOD_FAST_FILE: str = "result_good_fast.txt"
RESULT_GOOD_SLOW_FILE: str = "result_good_slow.txt"
RESULT_BLOCKED_FAST_FILE: str = "result_blocked_fast.txt"


# ==========================
# Вспомогательные структуры
# ==========================


@dataclass
class ParsedNode:
    protocol: str
    raw: str
    host: Optional[str]
    port: Optional[int]
    uuid: Optional[str] = None
    password: Optional[str] = None
    method: Optional[str] = None
    network: Optional[str] = None
    security: Optional[str] = None
    sni: Optional[str] = None
    path: Optional[str] = None
    host_header: Optional[str] = None
    flow: Optional[str] = None
    fp: Optional[str] = None
    alpn: Optional[str] = None
    # Доп. поля под hy2/tuic можно добавлять по мере необходимости


@dataclass
class TestResult:
    node_string: str
    identity: str
    reachable: bool
    has_marker: bool
    download_mbps: Optional[float]


# ==========================
# Утилиты файлов и логирования
# ==========================


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def ensure_parent_dir(path: str) -> None:
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)


def write_atomic(path: str, data: str) -> None:
    """Атомарная запись файла через временный файл и os.replace."""
    ensure_parent_dir(path)
    dir_name = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".tmp_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(data)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ==========================
# Извлечение конфигов (industrial_extractor)
# ==========================


PROTOCOL_RE = re.compile(r'(?i)(?:vless|vmess|trojan|ss|hy2|tuic)://[^\s<>"\']+')
# Base64 блоки в подписках часто urlsafe (с '-' и '_') и с переносами строк.
BASE64_BLOCK_RE = re.compile(r"([A-Za-z0-9+/=_-]{80,})")


def _decode_base64_to_text(b64_str: str) -> Optional[str]:
    """Пробуем декодировать Base64/urlsafe-Base64 блок в текст UTF-8."""
    # Убираем пробелы/переводы строк
    compact = re.sub(r"\s+", "", b64_str)
    # Добавляем padding, если нужно
    padding = len(compact) % 4
    if padding:
        compact += "=" * (4 - padding)
    decoded: Optional[bytes] = None
    # 1) urlsafe
    try:
        decoded = base64.urlsafe_b64decode(compact)
    except Exception:
        decoded = None
    # 2) обычный base64
    if decoded is None:
        try:
            decoded = base64.b64decode(compact, validate=False)
        except Exception:
            return None
    try:
        return (decoded or b"").decode("utf-8", errors="ignore")
    except Exception:
        return None


def _maybe_decode_entire_subscription(text: str) -> Optional[str]:
    """
    Многие подписки — это одна большая base64-строка (особенно если URL не raw).
    Если похоже на base64 и нет '://', пробуем декодировать целиком.
    """
    stripped = re.sub(r"\s+", "", text).strip()
    if not stripped or len(stripped) < 200:
        return None
    if "://" in stripped:
        return None
    if not re.fullmatch(r"[A-Za-z0-9+/=_-]+", stripped):
        return None
    decoded = _decode_base64_to_text(stripped)
    if not decoded:
        return None
    # эвристика: в нормальном декоде появятся схемы или структурные ключи
    if "://" in decoded or "proxies" in decoded or "outbounds" in decoded:
        return decoded
    return None


def industrial_extractor(source_text: str) -> List[str]:
    """
    Извлекает все конфиги vless/vmess/trojan/ss/hy2/tuic из текста.
    Параллельно рекурсивно декодирует Base64-блоки и повторно ищет в них.
    """
    seen_nodes: set[str] = set()
    seen_b64: set[str] = set()
    results: List[str] = []

    queue: List[Tuple[str, int]] = [(source_text, 0)]
    whole_decoded = _maybe_decode_entire_subscription(source_text)
    if whole_decoded:
        queue.append((whole_decoded, 1))

    while queue:
        text, depth = queue.pop()
        if depth > 6:
            continue

        # Прямые URL-конфиги
        for match in PROTOCOL_RE.finditer(text):
            node = match.group(0).strip()
            if node and node not in seen_nodes:
                seen_nodes.add(node)
                results.append(node)

        # Поиск и декодирование Base64-блоков
        for match in BASE64_BLOCK_RE.finditer(text):
            b64_block = match.group(1)
            if b64_block in seen_b64:
                continue
            seen_b64.add(b64_block)
            decoded = _decode_base64_to_text(b64_block)
            if not decoded:
                continue
            # небольшая эвристика: в decoded должны появиться схемы
            if "://" not in decoded and "proxies" not in decoded and "outbounds" not in decoded:
                continue
            queue.append((decoded, depth + 1))

    return results


# ==========================
# Парсинг конфигов и identity
# ==========================


def _safe_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


SS_URI_RE = re.compile(
    r"(?P<method>[^:]+):(?P<password>[^@]+)@(?P<host>[^:]+):(?P<port>\d+)"
)


def parse_ss(node_str: str) -> Optional[ParsedNode]:
    """
    Простейший разбор SS:
    - ss://base64(method:password@host:port)#tag
    - ss://method:password@host:port#tag
    """
    try:
        without_scheme = node_str.split("://", 1)[1]
    except IndexError:
        return None

    # Отрезаем фрагмент
    main_part = without_scheme.split("#", 1)[0]

    candidate = main_part
    if "@" not in candidate:
        decoded = _decode_base64_to_text(candidate)
        if decoded and "@" in decoded:
            candidate = decoded

    match = SS_URI_RE.search(candidate)
    if not match:
        return None

    method = match.group("method")
    password = match.group("password")
    host = match.group("host")
    port = _safe_int(match.group("port"))

    return ParsedNode(
        protocol="ss",
        raw=node_str,
        host=host,
        port=port,
        method=method,
        password=password,
        network="tcp",
    )


def parse_node(node_str: str) -> Optional[ParsedNode]:
    """Определяет протокол и парсит конфиг в нормализованную структуру."""
    node_str = node_str.strip()
    if not node_str:
        return None

    if "://" not in node_str:
        return None

    scheme = node_str.split("://", 1)[0].lower()

    # Отдельная логика для vmess и ss
    if scheme == "vmess":
        b64_payload = node_str.split("://", 1)[1].strip()
        decoded = _decode_base64_to_text(b64_payload)
        if not decoded:
            return None
        try:
            data = json.loads(decoded)
        except json.JSONDecodeError:
            return None

        host = data.get("add") or data.get("address")
        port = _safe_int(str(data.get("port"))) if data.get("port") is not None else None
        uuid = data.get("id")

        network = data.get("net") or "tcp"
        security = data.get("tls") or data.get("security") or None
        sni = data.get("sni") or data.get("host") or host
        path = data.get("path") or "/"
        flow = data.get("flow")
        fp = data.get("fp")

        return ParsedNode(
            protocol="vmess",
            raw=node_str,
            host=host,
            port=port,
            uuid=uuid,
            network=network,
            security=security,
            sni=sni,
            path=path,
            host_header=data.get("host"),
            flow=flow,
            fp=fp,
        )

    if scheme == "ss":
        return parse_ss(node_str)

    # Общий случай для vless/trojan/hy2/tuic
    parsed = urlparse(node_str)
    host = parsed.hostname
    port = parsed.port
    query = parse_qs(parsed.query)

    network = query.get("type", [None])[0] or query.get("net", [None])[0] or "tcp"
    security = query.get("security", [None])[0] or None
    sni = query.get("sni", [None])[0] or query.get("host", [None])[0] or host
    path = query.get("path", [None])[0] or parsed.path or "/"
    flow = query.get("flow", [None])[0]
    fp = query.get("fp", [None])[0]
    alpn = query.get("alpn", [None])[0]

    username = parsed.username
    password = parsed.password

    if scheme == "vless":
        uuid = username or password
        return ParsedNode(
            protocol="vless",
            raw=node_str,
            host=host,
            port=port,
            uuid=uuid,
            network=network,
            security=security,
            sni=sni,
            path=path,
            host_header=query.get("host", [None])[0],
            flow=flow,
            fp=fp,
            alpn=alpn,
        )

    if scheme == "trojan":
        pwd = username or password
        return ParsedNode(
            protocol="trojan",
            raw=node_str,
            host=host,
            port=port,
            password=pwd,
            network=network,
            security=security or "tls",
            sni=sni,
            fp=fp,
            alpn=alpn,
        )

    if scheme == "hy2":
        pwd = username or password
        return ParsedNode(
            protocol="hy2",
            raw=node_str,
            host=host,
            port=port,
            password=pwd,
            network="udp",
            security=security,
            sni=sni,
            fp=fp,
            alpn=alpn,
        )

    if scheme == "tuic":
        pwd = username or password
        return ParsedNode(
            protocol="tuic",
            raw=node_str,
            host=host,
            port=port,
            password=pwd,
            network="udp",
            security=security,
            sni=sni,
            fp=fp,
            alpn=alpn,
        )

    # Неподдерживаемая схема
    return None


def extract_server_identity(parsed: ParsedNode) -> Optional[str]:
    """
    Формирует identity для дедупликации.

    Важно: dedupe ТОЛЬКО по host:port приведёт к схлопыванию десятков тысяч строк в пару
    серверов (часто один и тот же endpoint, но разные UUID/пароли/транспорты).
    Поэтому identity включает минимум ключевых параметров.
    """
    if not parsed.host or not parsed.port:
        return None
    network = (parsed.network or "tcp").lower()
    sni = (parsed.sni or "").lower()
    security = (parsed.security or "").lower()
    path = parsed.path or ""

    # Учет "учётки" (uuid/password/method)
    account = (
        parsed.uuid
        or parsed.password
        or (f"{parsed.method}:{parsed.password}" if parsed.protocol == "ss" and parsed.method and parsed.password else "")
        or ""
    )

    # Собираем компактный стабильный ключ
    key = "|".join(
        [
            parsed.protocol,
            parsed.host.lower(),
            str(parsed.port),
            network,
            security,
            sni,
            path,
            account,
            parsed.flow or "",
            parsed.host_header or "",
        ]
    )

    # Чтобы identity не раздувал файлы, хэшируем хвост
    digest = hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"{parsed.host}:{parsed.port}:{parsed.protocol}:{network}:{digest}"


# ==========================
# Загрузка подписок и нормализация
# ==========================


def download_raw_subscriptions() -> str:
    """Скачивает все RAW-подписки и возвращает объединённый текст."""
    parts: List[str] = []
    for url in RAW_SUBSCRIPTION_URLS:
        try:
            logging.info("Скачиваю подписку %s", url)
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            parts.append(resp.text)
        except Exception as exc:
            logging.warning("Не удалось скачать %s: %s", url, exc)
    return "\n".join(parts)


def read_existing_normalized() -> Dict[str, str]:
    """Читает существующий normalized_subscription.txt (если есть)."""
    result: Dict[str, str] = {}
    if not os.path.exists(LOCAL_SUB_FILE):
        return result
    try:
        with open(LOCAL_SUB_FILE, "r", encoding="utf-8") as f:
            for line in f:
                node = line.strip()
                if not node:
                    continue
                parsed = parse_node(node)
                if not parsed:
                    continue
                identity = extract_server_identity(parsed)
                if not identity:
                    continue
                result[identity] = node
    except Exception as exc:
        logging.warning("Не удалось прочитать %s: %s", LOCAL_SUB_FILE, exc)
    return result


def build_normalized_subscription(raw_text: str) -> Tuple[Dict[str, str], Dict[str, ParsedNode]]:
    """
    Из RAW-текста и существующего normalized формирует объединённый словарь
    identity -> node_string и параллельно возвращает разобранные ParsedNode.
    """
    nodes_by_identity: Dict[str, str] = {}
    parsed_by_identity: Dict[str, ParsedNode] = {}

    # 1) Берём уже существующие нормализованные узлы (живые с прошлого прогона)
    existing = read_existing_normalized()
    nodes_by_identity.update(existing)

    # 2) Добавляем новые из RAW-подписок
    extracted = industrial_extractor(raw_text)
    logging.info("Извлечено %d конфигов из RAW-подписок (до парсинга/дедупа)", len(extracted))

    for node in extracted:
        parsed = parse_node(node)
        if not parsed:
            continue
        identity = extract_server_identity(parsed)
        if not identity:
            continue
        # новые узлы из RAW перекрывают старые с тем же identity
        nodes_by_identity[identity] = node
        parsed_by_identity[identity] = parsed

    # 3) Для существующих узлов, которые не были перекрыты RAW, распарсим тоже
    for identity, node in nodes_by_identity.items():
        if identity in parsed_by_identity:
            continue
        parsed = parse_node(node)
        if parsed:
            parsed_by_identity[identity] = parsed

    # 4) Записываем текущий нормализованный список (все уникальные узлы)
    normalized_lines = "\n".join(nodes_by_identity.values())
    if normalized_lines:
        normalized_lines += "\n"
    write_atomic(LOCAL_SUB_FILE, normalized_lines)
    logging.info(
        "Нормализованный список узлов сохранён в %s (%d шт.)",
        LOCAL_SUB_FILE,
        len(nodes_by_identity),
    )

    return nodes_by_identity, parsed_by_identity


# ==========================
# Построение конфига Xray
# ==========================


def build_stream_settings(parsed: ParsedNode) -> Dict:
    network = (parsed.network or "tcp").lower()
    security = (parsed.security or "").lower()
    sni = parsed.sni or parsed.host

    stream_settings: Dict = {"network": network}

    if security in {"tls", "reality"}:
        fp = parsed.fp or random.choice(TLS_FINGERPRINTS)
        tls_settings: Dict = {
            "serverName": sni,
            "fingerprint": fp,
        }
        stream_settings["security"] = "tls"
        stream_settings["tlsSettings"] = tls_settings

    if network == "ws":
        ws_settings: Dict = {
            "path": parsed.path or "/",
            "headers": {},
        }
        if parsed.host_header:
            ws_settings["headers"]["Host"] = parsed.host_header
        elif sni:
            ws_settings["headers"]["Host"] = sni
        stream_settings["wsSettings"] = ws_settings
    elif network == "grpc":
        grpc_settings: Dict = {}
        stream_settings["grpcSettings"] = grpc_settings

    return stream_settings


def build_xray_config(parsed: ParsedNode, local_port: int) -> Dict:
    """
    Формирует минимально корректный Xray-конфиг с одним inbound (SOCKS5)
    и одним outbound под конкретный узел.
    """
    if not parsed.host or not parsed.port:
        raise ValueError("У узла нет host/port, невозможно построить конфиг Xray")

    outbound: Dict
    stream_settings = build_stream_settings(parsed)

    if parsed.protocol == "vmess":
        outbound = {
            "tag": "proxy",
            "protocol": "vmess",
            "settings": {
                "vnext": [
                    {
                        "address": parsed.host,
                        "port": parsed.port,
                        "users": [
                            {
                                "id": parsed.uuid,
                                "security": "auto",
                            }
                        ],
                    }
                ]
            },
            "streamSettings": stream_settings,
        }
    elif parsed.protocol == "vless":
        outbound = {
            "tag": "proxy",
            "protocol": "vless",
            "settings": {
                "vnext": [
                    {
                        "address": parsed.host,
                        "port": parsed.port,
                        "users": [
                            {
                                "id": parsed.uuid,
                                "flow": parsed.flow or "",
                            }
                        ],
                    }
                ]
            },
            "streamSettings": stream_settings,
        }
    elif parsed.protocol == "trojan":
        outbound = {
            "tag": "proxy",
            "protocol": "trojan",
            "settings": {
                "servers": [
                    {
                        "address": parsed.host,
                        "port": parsed.port,
                        "password": parsed.password or parsed.uuid,
                        "sni": parsed.sni or parsed.host,
                    }
                ]
            },
            "streamSettings": stream_settings,
        }
    elif parsed.protocol == "ss":
        outbound = {
            "tag": "proxy",
            "protocol": "shadowsocks",
            "settings": {
                "servers": [
                    {
                        "address": parsed.host,
                        "port": parsed.port,
                        "method": parsed.method or "aes-128-gcm",
                        "password": parsed.password or "",
                    }
                ]
            },
        }
    elif parsed.protocol == "hy2":
        outbound = {
            "tag": "proxy",
            "protocol": "hysteria2",
            "settings": {
                "servers": [
                    {
                        "address": parsed.host,
                        "port": parsed.port,
                        "password": parsed.password or parsed.uuid or "",
                        "sni": parsed.sni or parsed.host,
                    }
                ]
            },
        }
    elif parsed.protocol == "tuic":
        outbound = {
            "tag": "proxy",
            "protocol": "tuic",
            "settings": {
                "servers": [
                    {
                        "address": parsed.host,
                        "port": parsed.port,
                        "password": parsed.password or parsed.uuid or "",
                        "sni": parsed.sni or parsed.host,
                    }
                ]
            },
        }
    else:
        raise ValueError(f"Неподдерживаемый протокол: {parsed.protocol}")

    config: Dict = {
        "log": {
            "loglevel": "warning",
        },
        "inbounds": [
            {
                "tag": "socks-in",
                "listen": "127.0.0.1",
                "port": local_port,
                "protocol": "socks",
                "settings": {
                    "auth": "noauth",
                    "udp": True,
                },
            }
        ],
        "outbounds": [
            outbound,
        ],
    }

    return config


# ==========================
# Запуск Xray и проверка узла
# ==========================


async def wait_for_port(host: str, port: int, timeout: float, proc: asyncio.subprocess.Process) -> bool:
    """Ждёт, пока порт начнёт слушать, либо пока не истечёт тайм-аут/не упадёт Xray."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.returncode is not None:
            return False
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except (OSError, ConnectionError):
            await asyncio.sleep(0.3)
    return False


def build_random_headers() -> Dict[str, str]:
    ua = random.choice(USER_AGENTS)
    lang = random.choice(ACCEPT_LANGUAGES)
    headers = {
        "User-Agent": ua,
        "Accept-Language": lang,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "keep-alive",
    }
    # Иногда добавляем Referer
    if random.random() < 0.5 and TEST_URLS:
        headers["Referer"] = TEST_URLS[0]
    return headers


async def measure_download_speed_simple(
    client: httpx.AsyncClient,
    url: str,
    download_size_bytes: int,
) -> Optional[float]:
    """
    Вариант A: замер скорости по времени скачивания N байт через тот же прокси.
    Возвращает Mbps или None при ошибке.
    """
    try:
        start = time.monotonic()
        total = 0
        async with client.stream("GET", url, headers=build_random_headers()) as resp:
            async for chunk in resp.aiter_bytes():
                if not chunk:
                    break
                total += len(chunk)
                if total >= download_size_bytes:
                    break
        duration = time.monotonic() - start
        if duration <= 0 or total == 0:
            return None
        mbits = (total * 8) / 1_000_000.0
        return mbits / duration
    except Exception as exc:
        logging.warning("Ошибка замера скорости (simple): %s", exc)
        return None


def _librespeed_available() -> bool:
    return os.path.exists(LIBRESPEED_PATH) and os.access(LIBRESPEED_PATH, os.X_OK)


async def measure_download_speed_librespeed(local_port: int) -> Optional[float]:
    """
    Вариант B: запуск внешнего librespeed-cli через subprocess.
    Возвращает download Mbps или None при ошибке.
    """
    if not _librespeed_available():
        return None

    loop = asyncio.get_running_loop()

    def _run() -> Optional[float]:
        env = os.environ.copy()
        proxy = f"socks5h://127.0.0.1:{local_port}"
        env["ALL_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["HTTP_PROXY"] = proxy
        try:
            import subprocess

            completed = subprocess.run(
                [LIBRESPEED_PATH, "--json"],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if completed.returncode != 0:
                logging.warning(
                    "librespeed-cli завершился с кодом %s: %s",
                    completed.returncode,
                    completed.stderr,
                )
                return None
            data = json.loads(completed.stdout or "{}")
            # Формат зависит от версии, но обычно есть поле "download"
            download_mbps = data.get("download") or data.get("download_speed")
            if isinstance(download_mbps, (int, float)):
                return float(download_mbps)
        except Exception as exc:
            logging.warning("Ошибка запуска librespeed-cli: %s", exc)
        return None

    return await loop.run_in_executor(None, _run)


async def test_single_node(identity: str, parsed: ParsedNode, index: int) -> TestResult:
    """
    Поднимает Xray с конфигом для одного узла, проверяет доступность и скорость,
    затем останавливает Xray.
    """
    # Простой генератор локальных портов (в пределах диапазона, без сложных проверок)
    local_port = 50_000 + (index % 10_000)

    # Строим конфиг Xray
    try:
        config = build_xray_config(parsed, local_port)
    except Exception as exc:
        logging.warning("Не удалось построить конфиг Xray для %s: %s", identity, exc)
        return TestResult(parsed.raw, identity, False, False, None)

    # Сохраняем во временный файл
    fd, cfg_path = tempfile.mkstemp(prefix="xray_cfg_", suffix=".json")
    os.close(fd)
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        # Запускаем Xray
        proc = await asyncio.create_subprocess_exec(
            XRAY_PATH,
            "run",
            "-config",
            cfg_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        try:
            ok = await wait_for_port("127.0.0.1", local_port, XRAY_START_TIMEOUT, proc)
            if not ok:
                logging.warning("Xray не поднял порт %s для %s", local_port, identity)
                return TestResult(parsed.raw, identity, False, False, None)

            proxy_url = f"socks5://127.0.0.1:{local_port}"
            timeout = httpx.Timeout(REQUEST_TIMEOUT)

            await asyncio.sleep(random.uniform(*RANDOM_JITTER_RANGE))

            reachable = False
            has_marker = False
            download_mbps: Optional[float] = None

            async with httpx.AsyncClient(
                proxies=proxy_url,
                timeout=timeout,
                http2=True,
            ) as client:
                # Проверка целевого URL
                try:
                    if not TEST_URLS:
                        raise RuntimeError("TEST_URLS пуст")
                    test_url = TEST_URLS[0]
                    resp = await client.get(
                        test_url,
                        headers=build_random_headers(),
                        follow_redirects=True,
                    )
                    text = resp.text
                    reachable = True
                    has_marker = TARGET_OK_MARKER in text
                except Exception as exc:
                    logging.info("Не удалось получить TEST_URL для %s: %s", identity, exc)
                    reachable = False

                # Замер скорости, если до целевого URL достучались
                if reachable:
                    # Сначала пробуем librespeed-cli, затем fallback
                    download_mbps = await measure_download_speed_librespeed(local_port)
                    if download_mbps is None:
                        download_mbps = await measure_download_speed_simple(
                            client,
                            test_url,
                            DOWNLOAD_SIZE_BYTES,
                        )

            return TestResult(parsed.raw, identity, reachable, has_marker, download_mbps)
        finally:
            # Аккуратно останавливаем Xray
            try:
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        proc.kill()
            except ProcessLookupError:
                pass
    finally:
        try:
            os.remove(cfg_path)
        except OSError:
            pass


def classify_result(result: TestResult) -> Optional[str]:
    """
    Классификация узла:
    - good_fast  -> доступен + есть маркер + скорость >= порога
    - good_slow  -> доступен + есть маркер + скорость < порога
    - blocked_fast -> доступен + НЕТ маркера + скорость >= порога
    - иначе -> неинтересен (None)
    """
    if not result.reachable or result.download_mbps is None:
        return None
    if result.has_marker:
        return "good_fast" if result.download_mbps >= SPEED_FAST_MBPS else "good_slow"
    else:
        if result.download_mbps >= SPEED_FAST_MBPS:
            return "blocked_fast"
    return None


# ==========================
# Обновление файлов результатов
# ==========================


def _load_result_file(path: str) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    if not os.path.exists(path):
        return mapping
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                node = line.strip()
                if not node:
                    continue
                parsed = parse_node(node)
                if not parsed:
                    continue
                identity = extract_server_identity(parsed)
                if not identity:
                    continue
                mapping[identity] = node
    except Exception as exc:
        logging.warning("Не удалось прочитать файл результатов %s: %s", path, exc)
    return mapping


def update_result_files(results: Iterable[TestResult]) -> None:
    """Обновляет result_good_fast/slow/blocked_fast с учётом новых результатов."""
    good_fast = _load_result_file(RESULT_GOOD_FAST_FILE)
    good_slow = _load_result_file(RESULT_GOOD_SLOW_FILE)
    blocked_fast = _load_result_file(RESULT_BLOCKED_FAST_FILE)

    for res in results:
        identity = res.identity
        # "Мёртвые" узлы удаляем из всех файлов
        if not res.reachable:
            good_fast.pop(identity, None)
            good_slow.pop(identity, None)
            blocked_fast.pop(identity, None)
            continue

        category = classify_result(res)
        # Сначала убираем из всех
        if category != "good_fast":
            good_fast.pop(identity, None)
        if category != "good_slow":
            good_slow.pop(identity, None)
        if category != "blocked_fast":
            blocked_fast.pop(identity, None)

        # Затем добавляем в нужный
        if category == "good_fast":
            good_fast[identity] = res.node_string
        elif category == "good_slow":
            good_slow[identity] = res.node_string
        elif category == "blocked_fast":
            blocked_fast[identity] = res.node_string

    def _dump(path: str, mapping: Dict[str, str]) -> None:
        if not mapping:
            write_atomic(path, "")
            return
        # Стабильный порядок: сортируем по identity
        lines = [mapping[k] for k in sorted(mapping.keys())]
        content = "\n".join(lines) + "\n"
        write_atomic(path, content)

    _dump(RESULT_GOOD_FAST_FILE, good_fast)
    _dump(RESULT_GOOD_SLOW_FILE, good_slow)
    _dump(RESULT_BLOCKED_FAST_FILE, blocked_fast)


def update_normalized_from_results(results: Iterable[TestResult]) -> None:
    """Обновляет normalized_subscription.txt только живыми узлами (reachable=True)."""
    alive: Dict[str, str] = {}
    for res in results:
        if not res.reachable:
            continue
        alive[res.identity] = res.node_string

    if not alive:
        logging.warning("После прогона не осталось живых узлов.")
        write_atomic(LOCAL_SUB_FILE, "")
        return

    lines = [alive[k] for k in sorted(alive.keys())]
    content = "\n".join(lines) + "\n"
    write_atomic(LOCAL_SUB_FILE, content)
    logging.info(
        "Обновлён %s: живых узлов %d",
        LOCAL_SUB_FILE,
        len(alive),
    )


# ==========================
# Основной асинхронный движок
# ==========================


async def run_all_tests(nodes_by_identity: Dict[str, str], parsed_by_identity: Dict[str, ParsedNode]) -> List[TestResult]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TESTS)

    identities = list(nodes_by_identity.keys())
    random.shuffle(identities)

    results: List[TestResult] = []

    async def _worker(idx: int, identity: str) -> None:
        parsed = parsed_by_identity.get(identity)
        if not parsed:
            return

        async with semaphore:
            try:
                res = await asyncio.wait_for(
                    test_single_node(identity, parsed, idx),
                    timeout=NODE_OVERALL_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logging.warning("Тайм-аут теста узла %s", identity)
                res = TestResult(parsed.raw, identity, False, False, None)
            except Exception as exc:
                logging.warning("Ошибка теста узла %s: %s", identity, exc)
                res = TestResult(parsed.raw, identity, False, False, None)
            results.append(res)

    tasks = [
        asyncio.create_task(_worker(idx, identity))
        for idx, identity in enumerate(identities)
    ]

    if tasks:
        await asyncio.gather(*tasks)

    return results


async def async_main() -> None:
    setup_logging()
    logging.info("Старт проверки подписок")

    # 1. Загрузка RAW-подписок и сохранение копии
    raw_text = download_raw_subscriptions()
    write_atomic(RAW_COPY_FILE, raw_text)
    logging.info("RAW-подписки сохранены в %s (длина %d байт)", RAW_COPY_FILE, len(raw_text))

    # 2. Построение нормализованного списка узлов
    nodes_by_identity, parsed_by_identity = build_normalized_subscription(raw_text)
    if not nodes_by_identity:
        logging.warning("Не найдено ни одного узла для проверки.")
        return

    logging.info("К тестированию подготовлено %d уникальных узлов", len(nodes_by_identity))

    # 3. Параллельное тестирование узлов
    results = await run_all_tests(nodes_by_identity, parsed_by_identity)
    logging.info("Тестирование завершено. Получено результатов: %d", len(results))

    # 4. Обновление normalized_subscription.txt и result_*.txt
    update_normalized_from_results(results)
    update_result_files(results)

    # Немного статистики
    stats = {"good_fast": 0, "good_slow": 0, "blocked_fast": 0, "dead": 0}
    for r in results:
        if not r.reachable:
            stats["dead"] += 1
        else:
            cat = classify_result(r)
            if cat in stats:
                stats[cat] += 1
    logging.info(
        "Итог: good_fast=%d, good_slow=%d, blocked_fast=%d, dead=%d",
        stats["good_fast"],
        stats["good_slow"],
        stats["blocked_fast"],
        stats["dead"],
    )


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
