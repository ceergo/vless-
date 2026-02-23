/**
 * Ultra-Light Gemini & Xray Checker (GitHub Ready)
 * Senior Coding Standard: Robustness, Logging, Idempotency.
 * 23.02.2026: Добавлена автоматическая загрузка бинарника под архитектуру системы.
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
    // Прямые ссылки на релизы для x64 и ARM64
    binSources: {
        'x64': 'https://github.com/uS-S/ladspeed/releases/download/v0.6.0/ladspeed-linux-amd64',
        'arm64': 'https://github.com/uS-S/ladspeed/releases/download/v0.6.0/ladspeed-linux-arm64'
    },
    testTimeout: 20000,
    geminiUrl: 'https://gemini.google.com',
    portRange: { min: 30100, max: 30900 }
};

// --- ЛОГИРОВАНИЕ ---
const log = (msg, type = 'INFO') => {
    const timestamp = new Date().toISOString().replace('T', ' ').substring(0, 19);
    const colors = { INFO: '\x1b[36m', SUCCESS: '\x1b[32m', WARN: '\x1b[33m', ERROR: '\x1b[31m', DEBUG: '\x1b[90m', TRACE: '\x1b[95m' };
    console.log(`${colors[type] || ''}[${timestamp}] [${type}] ${msg}\x1b[0m`);
};

// --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
function getRandomPort() {
    return Math.floor(Math.random() * (CONFIG.portRange.max - CONFIG.portRange.min + 1)) + CONFIG.portRange.min;
}

/**
 * Компактная загрузка файла с поддержкой редиректов
 */
async function downloadFile(url, dest) {
    return new Promise((resolve, reject) => {
        https.get(url, (res) => {
            if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
                return downloadFile(res.headers.location, dest).then(resolve).catch(reject);
            }
            if (res.statusCode !== 200) return reject(new Error(`HTTP ${res.statusCode}`));
            
            const stream = fs.createWriteStream(dest);
            res.pipe(stream);
            stream.on('finish', () => stream.close(resolve));
            stream.on('error', (err) => { fs.unlink(dest, () => reject(err)); });
        }).on('error', reject);
    });
}

/**
 * Обеспечивает наличие рабочего бинарника
 */
async function ensureBinary() {
    if (!fs.existsSync(CONFIG.binDir)) fs.mkdirSync(CONFIG.binDir, { recursive: true });

    const arch = process.arch === 'arm64' ? 'arm64' : 'x64';
    const url = CONFIG.binSources[arch];
    
    // Проверка: если файл есть и он не пустой (>1MB), пропускаем загрузку
    if (fs.existsSync(CONFIG.ladspeedBin) && fs.statSync(CONFIG.ladspeedBin).size > 1024 * 1024) {
        log(`Бинарник (${arch}) уже готов.`, 'SUCCESS');
        return;
    }

    log(`Загрузка бинарника (${arch}) из GitHub...`);
    try {
        if (fs.existsSync(CONFIG.ladspeedBin)) fs.unlinkSync(CONFIG.ladspeedBin);
        await downloadFile(url, CONFIG.ladspeedBin);
        fs.chmodSync(CONFIG.ladspeedBin, '755');
        log('Бинарник успешно скачан и подготовлен.', 'SUCCESS');
    } catch (e) {
        log(`Ошибка загрузки: ${e.message}`, 'ERROR');
        process.exit(1);
    }
}

// --- ИНИЦИАЛИЗАЦИЯ ---
async function init() {
    log('Инициализация системы...');
    
    try {
        execSync('pkill -9 ladspeed', { stdio: 'ignore' });
    } catch (e) {}

    // Гарантируем наличие бинарника перед созданием файлов
    await ensureBinary();

    Object.values(CONFIG.files).forEach(file => {
        if (!fs.existsSync(file)) fs.writeFileSync(file, '');
        try { fs.chmodSync(file, '666'); } catch(e) {}
    });

    try {
        const fileInfo = execSync(`file ${CONFIG.ladspeedBin}`).toString();
        log(`Диагностика: ${fileInfo.trim()}`, 'DEBUG');
    } catch (e) {
        log(`Диагностика не удалась: ${e.message}`, 'WARN');
    }
}

// --- УМНЫЙ ПАРСИНГ ---
async function fetchSubscription() {
    const url = process.env.SUB_URL || CONFIG.defaultSubUrl;
    log(`Загрузка подписки: ${url}`);
    
    return new Promise((resolve, reject) => {
        https.get(url, (res) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => {
                let content = data;
                if (!data.includes('://') && data.length > 20) {
                    try {
                        content = Buffer.from(data, 'base64').toString('utf-8');
                        log('Декодирование Base64 успешно.');
                    } catch (e) {
                        log('Base64 ошибка, читаем как текст.', 'WARN');
                    }
                }
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
    const proxyUrl = `http://127.0.0.1:${port}`;
    const agent = new HttpProxyAgent(proxyUrl);
    
    log(`[STEP 2] HTTP Запрос к Gemini через порт ${port}...`, 'TRACE');

    try {
        const response = await axios.get(CONFIG.geminiUrl, {
            httpAgent: agent,
            httpsAgent: agent,
            timeout: 15000, 
            validateStatus: null,
            maxRedirects: 10,
            headers: {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5'
            }
        });

        const finalUrl = response.request.res.responseUrl || response.config.url || '';
        log(`[STEP 3] HTTP Ответ получен. Status: ${response.status}, Final URL: ${finalUrl}`, 'TRACE');

        if (finalUrl.includes('/app')) {
            return { ok: true, url: finalUrl, status: response.status };
        }
        
        return { ok: false, url: finalUrl, status: response.status, msg: 'Маркер /app не найден в редиректе' };
    } catch (err) {
        let errMsg = err.message;
        if (err.code === 'ECONNREFUSED') errMsg = `Порт ${port} закрыт (Туннель не поднялся)`;
        if (err.code === 'ETIMEDOUT' || err.code === 'ECONNABORTED') errMsg = `Таймаут соединения через прокси`;
        
        return { ok: false, error: errMsg, code: err.code };
    }
}

// --- ТЕСТ КОНФИГА ---
async function testConfig(line, port) {
    return new Promise((resolve) => {
        try { execSync(`fuser -k ${port}/tcp`, { stdio: 'ignore' }); } catch(e) {}

        log(`[STEP 1] Запуск бинарника на порту ${port}...`, 'TRACE');
        
        const command = `"${CONFIG.ladspeedBin}" -p ${port} -config-line "${line.replace(/"/g, '\\"')}"`;
        
        const proc = spawn(command, [], {
            detached: true,
            shell: true, 
            stdio: ['pipe', 'pipe', 'pipe']
        });

        let binaryLogs = '';
        const startTime = Date.now();
        
        const onData = (data) => {
            const str = data.toString();
            binaryLogs += str;
            if (str.toLowerCase().includes('error') || str.toLowerCase().includes('fail') || str.toLowerCase().includes('invalid')) {
                log(`[BINARY LOG] ${str.trim()}`, 'DEBUG');
            }
        };

        proc.stderr.on('data', onData);
        proc.stdout.on('data', onData);

        const cleanup = () => {
            try { 
                process.kill(-proc.pid, 'SIGKILL'); 
            } catch(e) { 
                try { proc.kill('SIGKILL'); } catch(e2) {}
            }
        };

        let isResolved = false;

        const startWait = setTimeout(async () => {
            if (isResolved) return;

            log(`[STEP 1.5] Пауза на прогрев завершена. Проверка порта ${port}...`, 'DEBUG');
            
            const result = await checkGemini(port);
            
            if (!result.ok && result.error) {
                log(`Диагностика неудачи: ${result.error}`, 'ERROR');
                if (binaryLogs) {
                    log('Лог бинарника (последние 1000 симв):', 'DEBUG');
                    console.log(`\n${binaryLogs.slice(-1000).trim()}\n`);
                }
            }

            cleanup();
            isResolved = true;
            resolve(result);
        }, 8000); 

        proc.on('error', (err) => {
            if (isResolved) return;
            log(`КРИТИЧЕСКАЯ ОШИБКА SPAWN: ${err.message}`, 'ERROR');
            clearTimeout(startWait);
            cleanup();
            isResolved = true;
            resolve({ ok: false, error: err.message });
        });

        proc.on('exit', (code) => {
            if (isResolved) return;
            
            const duration = Date.now() - startTime;
            if (duration < 1500) {
                log(`Бинарник мгновенно завершился (код: ${code}, время: ${duration}мс).`, 'ERROR');
                if (binaryLogs) {
                    log('Вывод бинарника:', 'WARN');
                    console.log(`>>> ${binaryLogs.trim()}`);
                }
                clearTimeout(startWait);
                isResolved = true;
                resolve({ ok: false, error: `Quick exit with code ${code}` });
            }
        });
    });
}

// --- MAIN ---
async function main() {
    await init();

    const allConfigs = await fetchSubscription();
    const deadHashes = getHashesFromFile(CONFIG.files.dead);
    const geminiHashes = getHashesFromFile(CONFIG.files.gemini);
    
    const tasks = allConfigs.filter(line => {
        const hash = crypto.createHash('md5').update(line).digest('hex');
        return !deadHashes.has(hash) && !geminiHashes.has(hash);
    });

    log(`Подписка: ${allConfigs.length} | К проверке: ${tasks.length}`);

    if (tasks.length === 0) {
        log('Новых конфигов нет. Выход.', 'SUCCESS');
        process.exit(0);
    }

    let currentGemini = fs.readFileSync(CONFIG.files.gemini, 'utf-8').split('\n').filter(Boolean);

    for (let i = 0; i < tasks.length; i++) {
        const line = tasks[i];
        const currentPort = getRandomPort();
        
        log(`[${i + 1}/${tasks.length}] === ТЕСТ (Порт: ${currentPort}) ===`);

        const res = await testConfig(line, currentPort);

        if (res.ok) {
            log(`[RESULT] SUCCESS! Доступ подтвержден.`, 'SUCCESS');
            currentGemini.push(line);
            fs.writeFileSync(CONFIG.files.gemini, [...new Set(currentGemini)].join('\n'));
        } else {
            const reason = res.error || res.msg || `Status ${res.status}`;
            log(`[RESULT] FAILED. Причина: ${reason}`, 'WARN');
            addToDead(line);
        }
    }

    log(`Проверка окончена. Итого живых: ${currentGemini.length}`, 'SUCCESS');
    
    setTimeout(() => {
        try { execSync('pkill -9 ladspeed', { stdio: 'ignore' }); } catch(e) {}
        process.kill(process.pid, 'SIGKILL'); 
    }, 1000);
}

main().catch(e => {
    log(`Критический сбой: ${e.message}`, 'ERROR');
    process.exit(1);
});
