# Edits Studio — Go

Управление монтажом полностью на Go. Один исполняемый файл, встроенные шрифты Montserrat, стандартная библиотека без сторонних Go-модулей и CGO. Python и виртуальное окружение для работы не нужны. Кодирование и фильтры выполняет внешний FFmpeg с libx264/libass.

## Быстрый запуск

Текущая Windows-сборка: `.local/bin/edits.exe`, FFmpeg рядом. Исполняемые файлы не входят в git.

```powershell
.\.local\bin\edits.exe doctor
.\.local\bin\edits.exe ingest "D:\Movies\movie.mkv"
.\.local\bin\edits.exe analyze "D:\Movies\movie.mkv"
.\.local\bin\edits.exe render --preview --profile accent examples/story.json
.\.local\bin\edits.exe render --profile accent examples/story.json
.\.local\bin\edits.exe check "PATH\video.mp4"
.\.local\bin\edits.exe approve --reviewer Mikhail --reviewed-video-and-audio SHA256
.\.local\bin\edits.exe queue --at "2026-12-01T09:00:00+03:00" SHA256
.\.local\bin\edits.exe status
```

Флаги команды указываются **перед** позиционными аргументами. Шаблон плана необходимо заполнить реальным материалом. `queue` создаёт только локальный план: загрузчик YouTube отсутствует. По решению пользователя канал и OAuth откладываются до локальной обкатки.

## Сборка и тесты

Go 1.23+ по go.mod; локально проверено на официальном Go 1.27.4. Для сборки не нужны Python, Node и C-компилятор.

```powershell
go test ./...
go vet ./...
go build -trimpath -o edits.exe ./cmd/edits
$env:EDITS_FFMPEG = "D:\Tools\ffmpeg.exe"
$env:EDITS_INTEGRATION = "1"
go test -count=1 -v ./internal/studio -run TestFFmpegIntegration
```

Если Go не установлен, `scripts/install-go.ps1` скачивает официальный portable SDK внутрь `.local`, проверяет SHA-256 и не меняет системный PATH. На этом компьютере запуск скриптов ограничен политикой Windows; использовать отдельный процесс, без изменения системной политики:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install-go.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 test ./...
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 build -trimpath -o .local/bin/edits.exe ./cmd/edits
```

FFmpeg ищется через EDITS_FFMPEG, PATH, рядом с edits.exe или в `.local/bin`. Go не ускоряет сам кодек: основная стоимость рендера остаётся в FFmpeg. Измерять выигрыш нужно на одинаковых исходниках, фильтрах и настройках кодирования.

## Рабочий процесс

- Карта смен планов всего исходника, кэш SHA-256 по пути/размеру/mtime. Это техническое сканирование, не полный смысловой просмотр фильма.
- Один JSON-план: самостоятельная история, оценка и причины отбора, интервалы, дорожка, титры, музыка и её происхождение.
- Предпросмотр 540×960, финал 1080×1920. Музыка приглушается на речи. Одно финальное кодирование, без обязательного AE.
- Текст: clean/accent/cinema/intrigue. Пользователю понравился accent. Анимации: мягкое появление и показ по словам с настоящими таймингами. Акценты сцены: короткое приближение и фокус на выбранной детали. Только явная расстановка с редакторской причиной.
- Неизменные ревизии, кэш, блокировка одновременного рендера одного плана. `batch --jobs 2 PLAN1 PLAN2` запускает ограниченное число независимых заданий (допустимо 1–4).
- Полное декодирование, чёрные кадры/зависания, длительность и звук. Ошибка повторной QA отзывает согласование. Чужая/устаревшая квитанция экспорта не допускается.
- Согласование привязано к точному SHA-256. Дубли исходника и интервалов, одинаковые слоты и изменённые файлы блокируются. Смысловые дубли со сдвинутыми границами требуют ручной сверки.
- Демонстрации не регистрируются как готовые к публикации. Ни рендер, ни проверка не обращаются к YouTube.

## Локальное движение

```powershell
.\.local\bin\edits.exe demo
```

Страница: `.local/go-motion-demo/index.html`. Три настоящих MP4 сравнивают показ текста по речи, приближение на ответе и фокус на детали. Демо использует сгенерированный портрет с цифровым наездом и Windows Irina; это не сыгранная сцена фильма. Тайминги слов получены от локального TTS, не угаданы по длине строки. На других ОС можно предоставить WAV/TSV. Непрерывное прослушивание человеком не заменяется автоматическими тестами.

## Что пока не готово

Автоматические ASR реального фильма, смысловой отбор всего фильма, кадрирование лиц, GUI, аналитика и публикация. Пока это локальное ядро с ручными планами и тестовыми демонстрациями. Для постановочной скорости и качества нужен следующий тест на настоящем материале.

Python-прототип сохранён в [archive/python-core-2026-11-16](https://github.com/Ogresssor/edits/tree/archive/python-core-2026-11-16), первоначальный проект — в [archive/legacy-2026-11-15](https://github.com/Ogresssor/edits/tree/archive/legacy-2026-11-15). Из текущей версии Python-исходники удалены. Старый SQLite не импортируется автоматически; новая локальная очередь начинается отдельно в `.local/go`.

[Архитектура](docs/architecture.md) · [Протокол перехода](docs/go-migration-2026-11-16.md) · [Оформление](docs/visual-direction.md)
