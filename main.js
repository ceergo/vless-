/**
 * Ultra-Light Gemini & Xray Checker (GitHub Ready)
 * Senior Coding Standard: Robustness, Logging, Idempotency.
 */

const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const https = require('https');
const crypto = require('crypto');
const axios = require('axios');
const { HttpProxyAgent } = require('http-proxy-agent');

// --- КОНФИГУРАЦИЯ ---
const CONFIG = {
    // Твоя реальная ссылка на подписку
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
    
    if (!fs.existsSync(CONFIG.binDir)) fs.mkdirSync(CONFIG.binDir);
    
    Object.values(CONFIG.files).forEach(file => {
        if (!fs.existsSync(file)) fs.writeFileSync(file, '');
        // Даем права на чтение/запись всем, чтобы git мог закомитить из-под root
        try { fs.chmodSync(file, '666'); } catch(e) {}
    });

    if (!fs.existsSync(CONFIG.ladspeedBin)) {
        log(`КРИТИЧЕСКАЯ ОШИБКА: Бинарник ${CONFIG.ladspeedBin} не найден!`, 'ERROR');
        process.exit(1);
    }
    
    try {
        const uid = process.getuid();
        if (uid !== 0) log('Запуск не от ROOT. Работа с DNS может быть ограничена.', 'WARN');
    } catch (e) {}
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
                // Проверяем, не Base64 ли это
                if (!data.includes('://') && data.length > 20) {
                    try {
                        content = Buffer.from(data, 'base64').toString('utf-8');
                        log('Обнаружен формат Base64, декодирование успешно.');
                    } catch (e) {
                        log('Не удалось декодировать Base64, используем как текст.', 'WARN');
                    }
                }
                const lines = content.split(/\r?\n/)
                    .map(l => l.trim())
                    .filter(l => l.includes('://'));
                resolve(lines);
            });
        }).on('error', reject);
    });
}

// --- БАЗА ХЕШЕЙ ---
function getHashesFromFile(filePath) {
    if (!fs.existsSync(filePath)) return new Set();
    const data = fs.readFileSync(filePath, 'utf-8');
    return new Set(data.split('\n').filter(Boolean).map(line => {
        // Если это файл dead.txt, там лежат чистые хеши. Если gemini.txt - там ссылки, нужно хешировать.
        if (filePath.endsWith('dead.txt')) return line.trim();
        return crypto.createHash('md5').update(line.trim()).digest('hex');
    }));
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
            stdio: ['ignore', 'pipe', 'pipe']
        });

        let binaryLogs = '';
        proc.stderr.on('data', (data) => binaryLogs += data.toString());
        proc.stdout.on('data', (data) => binaryLogs += data.toString());

        const cleanup = () => {
            try { 
                process.kill(-proc.pid, 'SIGKILL'); 
            } catch(e) { 
                proc.kill('SIGKILL'); 
            }
        };

        let isResolved = false;

        const startWait = setTimeout(async () => {
            if (isResolved) return;
            const result = await checkGemini(port);
            
            if (!result.ok && result.error && result.error.includes('ECONNREFUSED')) {
                log(`Бинарник не открыл порт 30005. Полный лог бинарника:\n${binaryLogs}`, 'ERROR');
            }

            cleanup();
            isResolved = true;
            resolve(result);
        }, 5000);

        proc.on('error', (err) => {
            if (isResolved) return;
            log(`Ошибка старта бинарника: ${err.message}`, 'ERROR');
            clearTimeout(startWait);
            cleanup();
            isResolved = true;
            resolve({ ok: false, error: err.message });
        });

        proc.on('exit', (code) => {
            if (isResolved || code === null || code === 0) return;
            log(`Бинарник неожиданно завершился с кодом ${code}. Лог:\n${binaryLogs}`, 'WARN');
        });
    });
}

// --- MAIN ---
async function main() {
    init();

    // Загружаем список из подписки
    const incomingConfigs = await fetchSubscription();
    
    // Загружаем хеши уже известных "мертвых" и уже найденных "Gemini"
    const deadHashes = getHashesFromFile(CONFIG.files.dead);
    const existingGeminiHashes = getHashesFromFile(CONFIG.files.gemini);
    
    const results = { 
        gemini: fs.readFileSync(CONFIG.files.gemini, 'utf-8').split('\n').filter(Boolean), 
        general: [] 
    };

    log(`Начинаем проверку. Всего в подписке: ${incomingConfigs.length}`);
    log(`Уже в базе: Gemini=${existingGeminiHashes.size}, Dead=${deadHashes.size}`);

    for (let i = 0; i < incomingConfigs.length; i++) {
        const line = incomingConfigs[i];
        const hash = crypto.createHash('md5').update(line).digest('hex');

        // ЗАЩИТА ОТ ЗАЦИКЛИВАНИЯ И ПОВТОРОВ
        if (deadHashes.has(hash)) {
            log(`[${i+1}/${incomingConfigs.length}] Пропуск: Известен как мертвый`, 'DEBUG');
            continue;
        }
        
        if (existingGeminiHashes.size > 0 && existingGeminiHashes.has(hash)) {
            log(`[${i+1}/${incomingConfigs.length}] Пропуск: Уже проверен и находится в Gemini.txt`, 'DEBUG');
            continue;
        }

        const res = await testConfig(line, 30005);

        if (res.ok) {
            log(`[UP] ПОДОШЁЛ!`, 'SUCCESS');
            log(`  -> ВЫХОД: ${res.url}`, 'SUCCESS');
            results.gemini.push(line);
            // Добавляем в текущий набор хешей, чтобы не проверить дубликат в рамках одного запуска
            existingGeminiHashes.add(hash);
        } else {
            log(`[DOWN] ОТКЛОНЕН`, 'WARN');
            log(`  -> ПРИЧИНА: ${res.url || res.error || 'Нет маркера /app'}`, 'WARN');
            addToDead(line);
            deadHashes.add(hash);
        }
    }

    // Сохранение результатов (уникальных)
    const uniqueGemini = Array.from(new Set(results.gemini));
    fs.writeFileSync(CONFIG.files.gemini, uniqueGemini.join('\n'));
    
    log(`Проверка завершена. Итого Gemini: ${uniqueGemini.length}`, 'SUCCESS');
    
    // ГАРАНТИРОВАННОЕ ЗАВЕРШЕНИЕ ПРОЦЕССА
    log('Завершение работы скрипта...');
    process.exit(0);
}

main().catch(e => {
    log(`Глобальная ошибка: ${e.message}`, 'ERROR');
    process.exit(1);
});
