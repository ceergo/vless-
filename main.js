/**
 * Ultra-Light Gemini & Xray Checker (GitHub Ready)
 * Senior Coding Standard: Robustness, Logging, Idempotency.
 */

const fs = require('fs');
const path = require('path');
const { spawn, execSync } = require('child_process');
const https = require('https');
const crypto = require('crypto');
const axios = require('axios');
const { HttpProxyAgent } = require('http-proxy-agent');

// --- КОНФИГУРАЦИЯ ---
const CONFIG = {
    defaultSubUrl: 'https://raw.githubusercontent.com/ceergo/parss/refs/heads/main/my_stable_configs.txt',
    files: {
        gemini: path.join(__dirname, 'gemini.txt'),
        general: path.join(__dirname, 'general.txt'),
        dead: path.join(__dirname, 'dead.txt')
    },
    binDir: path.join(__dirname, 'bin'),
    ladspeedBin: path.join(__dirname, 'bin', 'ladspeed'),
    testTimeout: 20000,
    geminiUrl: 'https://gemini.google.com'
};

// --- ЛОГИРОВАНИЕ ---
const log = (msg, type = 'INFO') => {
    const timestamp = new Date().toISOString().replace('T', ' ').substring(0, 19);
    const colors = { INFO: '\x1b[36m', SUCCESS: '\x1b[32m', WARN: '\x1b[33m', ERROR: '\x1b[31m', DEBUG: '\x1b[90m' };
    console.log(`${colors[type] || ''}[${timestamp}] [${type}] ${msg}\x1b[0m`);
};

// --- ИНИЦИАЛИЗАЦИЯ ---
function init() {
    log('Инициализация системы...');
    
    // Чистим старые процессы бинарника перед стартом, чтобы порт 30005 был свободен
    try {
        execSync('pkill -9 ladspeed', { stdio: 'ignore' });
        log('Старые процессы ladspeed зачищены.', 'DEBUG');
    } catch (e) {}

    if (!fs.existsSync(CONFIG.binDir)) fs.mkdirSync(CONFIG.binDir);
    
    Object.values(CONFIG.files).forEach(file => {
        if (!fs.existsSync(file)) fs.writeFileSync(file, '');
        try { fs.chmodSync(file, '666'); } catch(e) {}
    });

    if (!fs.existsSync(CONFIG.ladspeedBin)) {
        log(`КРИТИЧЕСКАЯ ОШИБКА: Бинарник ${CONFIG.ladspeedBin} не найден!`, 'ERROR');
        process.exit(1);
    }

    try {
        execSync(`chmod +x ${CONFIG.ladspeedBin}`);
        log('Права на выполнение ladspeed установлены.', 'DEBUG');
    } catch (e) {
        log(`Не удалось установить chmod +x: ${e.message}`, 'WARN');
    }
}

// --- УМНЫЙ ПАРСИНГ ---
async function fetchSubscription() {
    const url = process.env.SUB_URL || CONFIG.defaultSubUrl;
    log(`Загрузка подписки из: ${url}`);
    
    return new Promise((resolve, reject) => {
        https.get(url, (res) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => {
                let content = data;
                if (!data.includes('://') && data.length > 20) {
                    try {
                        content = Buffer.from(data, 'base64').toString('utf-8');
                        log('Обнаружен формат Base64, декодирование успешно.');
                    } catch (e) {
                        log('Не удалось декодировать Base64, используем как текст.', 'WARN');
                    }
                }
                // УНИКАЛИЗАЦИЯ: Оставляем только уникальные строки
                const lines = [...new Set(content.split(/\r?\n/)
                    .map(l => l.trim())
                    .filter(l => l.includes('://')))];
                resolve(lines);
            });
        }).on('error', reject);
    });
}

// --- БАЗА ХЕШЕЙ ---
function getHashesFromFile(filePath) {
    if (!fs.existsSync(filePath)) return new Set();
    const data = fs.readFileSync(filePath, 'utf-8');
    const lines = data.split('\n').map(l => l.trim()).filter(Boolean);
    
    if (filePath.endsWith('dead.txt')) return new Set(lines);
    
    return new Set(lines.map(line => crypto.createHash('md5').update(line).digest('hex')));
}

function addToDead(configLine) {
    const hash = crypto.createHash('md5').update(configLine).digest('hex');
    fs.appendFileSync(CONFIG.files.dead, hash + '\n');
}

// --- ПРОВЕРКА ГЕМИНИ ---
async function checkGemini(port) {
    const agent = new HttpProxyAgent(`http://127.0.0.1:${port}`);
    try {
        const response = await axios.get(CONFIG.geminiUrl, {
            httpAgent: agent,
            httpsAgent: agent,
            timeout: 10000,
            validateStatus: null,
            maxRedirects: 5,
            headers: {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
        });

        const finalUrl = response.request.res.responseUrl || '';
        if (finalUrl.includes('/app')) {
            return { ok: true, url: finalUrl };
        }
        return { ok: false, url: finalUrl };
    } catch (err) {
        return { ok: false, error: err.message };
    }
}

// --- ТЕСТ КОНФИГА ---
async function testConfig(line, port) {
    return new Promise((resolve) => {
        log(`[FULL_CONFIG] Передаем в бинарник: ${line}`, 'DEBUG');

        const proc = spawn(CONFIG.ladspeedBin, ['-p', port.toString(), '-config-line', line], {
            detached: true,
            stdio: ['pipe', 'pipe', 'pipe']
        });

        let binaryLogs = '';
        proc.stderr.on('data', (data) => binaryLogs += data.toString());
        proc.stdout.on('data', (data) => binaryLogs += data.toString());

        const cleanup = () => {
            try { 
                process.kill(-proc.pid, 'SIGKILL'); 
            } catch(e) { 
                try { proc.kill('SIGKILL'); } catch(e2) {}
            }
            // Дополнительная гарантия очистки порта
            try { execSync(`fuser -k ${port}/tcp`, { stdio: 'ignore' }); } catch(e) {}
        };

        let isResolved = false;

        const startWait = setTimeout(async () => {
            if (isResolved) return;
            
            const result = await checkGemini(port);
            
            if (!result.ok && result.error && result.error.includes('ECONNREFUSED')) {
                log(`Бинарник не открыл порт ${port}. Лог: ${binaryLogs.substring(0, 100).replace(/\n/g, ' ')}...`, 'ERROR');
            }

            cleanup();
            isResolved = true;
            resolve(result);
        }, 7000); // 7 секунд на запуск и проксирование

        proc.on('error', (err) => {
            if (isResolved) return;
            log(`КРИТИЧЕСКАЯ ОШИБКА SPAWN: ${err.message}`, 'ERROR');
            clearTimeout(startWait);
            cleanup();
            isResolved = true;
            resolve({ ok: false, error: err.message });
        });

        proc.on('exit', (code) => {
            if (!isResolved && code !== 0 && code !== null) {
                clearTimeout(startWait);
                isResolved = true;
                resolve({ ok: false, error: `Бинарник упал с кодом ${code}` });
            }
        });
    });
}

// --- MAIN ---
async function main() {
    init();

    // 1. Получаем уникальный список конфигов
    const allConfigs = await fetchSubscription();
    
    // 2. Загружаем историю
    const deadHashes = getHashesFromFile(CONFIG.files.dead);
    const geminiHashes = getHashesFromFile(CONFIG.files.gemini);
    
    // 3. ФИЛЬТРАЦИЯ (Исключаем то, что уже проверяли когда-либо)
    const tasks = allConfigs.filter(line => {
        const hash = crypto.createHash('md5').update(line).digest('hex');
        return !deadHashes.has(hash) && !geminiHashes.has(hash);
    });

    log(`Всего в подписке: ${allConfigs.length}`);
    log(`Пропуск проверенных: ${allConfigs.length - tasks.length}`);
    log(`К проверке сейчас: ${tasks.length}`);

    if (tasks.length === 0) {
        log('Новых ссылок для проверки не найдено.', 'SUCCESS');
        process.exit(0);
    }

    // Читаем текущие Gemini, чтобы дополнять файл
    let currentGemini = fs.readFileSync(CONFIG.files.gemini, 'utf-8').split('\n').filter(Boolean);

    // 4. ОДИНАРНЫЙ ПРОХОД (Strict Loop)
    for (let i = 0; i < tasks.length; i++) {
        const line = tasks[i];
        log(`[${i + 1}/${tasks.length}] Проверка...`);

        const res = await testConfig(line, 30005);

        if (res.ok) {
            log(`[UP] Gemini найден!`, 'SUCCESS');
            currentGemini.push(line);
            // Сохраняем прогресс сразу
            fs.writeFileSync(CONFIG.files.gemini, [...new Set(currentGemini)].join('\n'));
        } else {
            log(`[DOWN] Мимо: ${res.url || res.error || 'Check failed'}`, 'WARN');
            addToDead(line);
        }
    }

    log(`Проверка завершена. Найдено новых: ${currentGemini.length - geminiHashes.size}`, 'SUCCESS');
    
    // ГАРАНТИРОВАННОЕ ЗАВЕРШЕНИЕ
    log('Форсированный выход...');
    setTimeout(() => process.exit(0), 500);
}

main().catch(e => {
    log(`Глобальный сбой: ${e.message}`, 'ERROR');
    process.exit(1);
});
