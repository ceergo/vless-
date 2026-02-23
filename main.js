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
function getDeadHashes() {
    const data = fs.readFileSync(CONFIG.files.dead, 'utf-8');
    return new Set(data.split('\n').filter(Boolean));
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

        // Самое важное: мониторим финальный URL
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
        // Здесь мы предполагаем, что ladspeed умеет принимать ссылку в аргумент или через временный файл
        // Для примера: запуск бинарника
        const proc = spawn(CONFIG.ladspeedBin, ['-p', port.toString(), '-config-line', line], {
            detached: true
        });

        const cleanup = () => {
            try { process.kill(-proc.pid, 'SIGKILL'); } catch(e) { proc.kill('SIGKILL'); }
        };

        // Ждем инициализации туннеля
        setTimeout(async () => {
            const result = await checkGemini(port);
            cleanup();
            resolve(result);
        }, 3000);

        proc.on('error', (err) => {
            log(`Ошибка запуска Ladspeed: ${err.message}`, 'ERROR');
            resolve({ ok: false });
        });
    });
}

// --- MAIN ---
async function main() {
    init();

    const configs = await fetchSubscription();
    const deadHashes = getDeadHashes();
    const results = { gemini: [], general: [] };

    log(`Начинаем проверку ${configs.length} конфигураций...`);

    for (let i = 0; i < configs.length; i++) {
        const line = configs[i];
        const hash = crypto.createHash('md5').update(line).digest('hex');

        if (deadHashes.has(hash)) {
            log(`[${i+1}/${configs.length}] Пропуск (в черном списке)`, 'DEBUG');
            continue;
        }

        log(`[${i+1}/${configs.length}] Тестирование...`);
        const res = await testConfig(line, 30005);

        if (res.ok) {
            log(`[UP] Gemini доступен! (${res.url})`, 'SUCCESS');
            results.gemini.push(line);
        } else {
            // Если была ошибка сети или нет /app, считаем дохлым для Gemini
            log(`[DOWN] Не подошел: ${res.url || res.error || 'No /app marker'}`, 'WARN');
            addToDead(line);
        }
    }

    // Сохранение (перезапись только рабочими)
    fs.writeFileSync(CONFIG.files.gemini, results.gemini.join('\n'));
    // В general можно писать те, что просто работают, если добавить тест скорости
    fs.writeFileSync(CONFIG.files.general, results.general.join('\n'));

    log(`Готово! Gemini: ${results.gemini.length}, Dead: ${configs.length - results.gemini.length}`, 'SUCCESS');
}

main().catch(e => log(e.message, 'ERROR'));
