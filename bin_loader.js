const fs = require('fs');
const https = require('https');
const path = require('path');

// Ссылки на бинарники под разные архитектуры
const SOURCES = {
    'x64': {
        ladspeed: 'https://github.com/uS-S/ladspeed/releases/download/v0.6.0/ladspeed-linux-amd64',
        xray: 'https://github.com/XTLS/Xray-core/releases/download/v1.8.4/xray-linux-64'
    },
    'arm64': {
        ladspeed: 'https://github.com/uS-S/ladspeed/releases/download/v0.6.0/ladspeed-linux-arm64',
        xray: 'https://github.com/XTLS/Xray-core/releases/download/v1.8.4/xray-linux-arm64-v8a'
    }
};

/**
 * Внутренняя функция загрузки файла с поддержкой редиректов
 */
async function downloadFile(url, dest) {
    return new Promise((resolve, reject) => {
        https.get(url, (res) => {
            // Обработка редиректов (301, 302), типичных для GitHub Releases
            if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
                return downloadFile(res.headers.location, dest).then(resolve).catch(reject);
            }
            if (res.statusCode !== 200) return reject(new Error(`HTTP ${res.statusCode} for ${url}`));
            
            const stream = fs.createWriteStream(dest);
            res.pipe(stream);
            stream.on('finish', () => stream.close(resolve));
            stream.on('error', (err) => { fs.unlink(dest, () => reject(err)); });
        }).on('error', reject);
    });
}

/**
 * Основная экспортируемая функция для подготовки окружения
 * Теперь поддерживает загрузку нескольких бинарников (ladspeed + xray)
 */
async function ensureBinary(binDir, binPath, logger) {
    if (!fs.existsSync(binDir)) fs.mkdirSync(binDir, { recursive: true });

    const arch = process.arch === 'arm64' ? 'arm64' : 'x64';
    const targets = SOURCES[arch];
    
    // Определяем список файлов для проверки и загрузки
    const filesToLoad = [
        { name: 'ladspeed', url: targets.ladspeed, path: binPath },
        { name: 'xray', url: targets.xray, path: path.join(binDir, 'xray') }
    ];

    for (const file of filesToLoad) {
        // Проверка: если файл есть и он не пустой (>1MB), пропускаем
        if (fs.existsSync(file.path) && fs.statSync(file.path).size > 1024 * 1024) {
            logger(`Файл ${file.name} (${arch}) уже готов.`, 'SUCCESS');
            continue;
        }

        logger(`Загрузка ${file.name} (${arch}) из GitHub...`);
        try {
            if (fs.existsSync(file.path)) fs.unlinkSync(file.path);
            await downloadFile(file.url, file.path);
            fs.chmodSync(file.path, '755');
            logger(`Файл ${file.name} успешно скачан и подготовлен.`, 'SUCCESS');
        } catch (e) {
            logger(`Ошибка загрузки ${file.name}: ${e.message}`, 'ERROR');
            // Если критический бинарник не скачался, прерываем выполнение
            if (file.name === 'ladspeed') process.exit(1);
        }
    }
}

module.exports = { ensureBinary };
