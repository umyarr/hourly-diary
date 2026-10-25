# Архитектура реализованной v0.1.0

Архитектурная схема приложения. Telegram-бот, владелец, часовой пояс и провайдер распознавания задаются конфигурацией.

## Размещение и компоненты

Linux deployment использует один Python/aiogram/asyncio процесс под systemd и отдельный каталог данных. Приложению не требуются Docker, Redis, локальная STT-модель или публичный HTTP endpoint.

| Модуль | Ответственность |
| --- | --- |
| config | Typed config из env, owner/chat validation, лимиты, провайдер/модель/endpoint без чтения файлов секретов |
| commands | Декларативные 8 команд и reply keyboard; не переустанавливает Telegram profile |
| store | SQLite schema v1, сериализованные транзакции, inbox/offset, jobs, responses, backup API |
| core | Whitelist, текст/voice/replies/edits, corrections, команды, scheduler и reconciliation |
| runtime | Aiogram Bot, ручной polling, transport downloads, loops обработки/напоминаний/jobs/responses |
| media | Ограниченные download/STT jobs, Groq/OpenAI HTTP adapter и безопасные коды ошибок |
| exporting | Consistent metadata snapshot, Markdown/JSON/ZIP, audio SHA256 и manifest |
| sync | OpenSSH pull, bounded проверка ZIP, managed-block Obsidian merge и версии |
| maintenance / offhost | SQLite online backup, shared audio objects, restore и независимая локальная копия |
| __main__ | CLI run/commands/snapshot/backup/restore/import/pull/pull-backup |

Все серверные workers — задачи одного процесса. SQLite и ZIP/хеширование выполняются через ограниченный thread worker/to_thread, сеть — async. Один STT job одновременно. Время UTC в БД, локальная дата и отображение — Europe/Moscow.

## Поток и долговечность

```text
getUpdates → owner/private filter → SQLite inbox + offset (одна транзакция)
                                      ↓
                  entries + revisions + jobs + responses → inbox done
scheduler → durable unique slot → Telegram send → message ID + reconciliation
voice job → Telegram getFile/download → audio/UUID.ogg → STT → transcript/version
SQLite + неизменяемые audio → ZIP snapshot → SSH/SFTP → Windows → Daily/attachments
SQLite online backup + audio objects → server generations → independent local copy
```

Параллельного getUpdates/Dispatcher polling нет. Aiogram updates сериализуются с by_alias=True, иначе поле from_ не совпало бы с Telegram from. Перед следующим polling request update уже durable. Unauthorized body не попадает в inbox; обработанное raw body удаляется, update_id остаётся. Crash до process безопасен; повтор message/update не создаёт вторую запись.

Ответы владельцу также имеют durable очередь responses, связываются с update ID. Перед сетевой отправкой состояние sending; при неясном исходе unknown, не повторяется вслепую. Пользователь может повторить явную команду. Текст и голос получают короткое интерактивное подтверждение; это не автоматическое напоминание вне окна.

## Модель SQLite v1

- settings: offset, timezone, owner chat activation, paused, resume_after.
- inbox: уникальный update_id, filtered raw body, done.
- prompts: UUID, start/end/day, unique end, state, message ID.
- entries: UUID, уникальный message ID, prompt/parent/reply ID, day, message/received/edited times, kind, version, original/current text, corrected transcript, original_unknown, voice file IDs/MIME/duration/size, audio path/hash, status/transcript/error code.
- revisions: entry/version/update ID, исходник/правка/correction. Media/link обновления поднимают version, текстовые ревизии не подделывают.
- jobs: download/stt, entry unique для вида job, state/attempts/retry_at.
- responses: update ID, короткий текст или диапазон export, state.

SQLite WAL, synchronous FULL, foreign keys; соединение защищено RLock, операции короткие. На старте pending inbox остаётся, running jobs возвращаются pending, sending prompts/responses становятся unknown. Неизвестная версия схемы отвергается.

## Расписание и привязка

13 календарных границ ежедневно, 09:00…21:00 Moscow. В 09:00 вопрос о 08–09, в 21:00 о 20–21. Запуск отправки только в минуту HH:00; повтор scheduler в той же минуте не создаёт новый slot. После пропуска не догонять. `/pause` долговечна, `/resume` исключает текущую уже начавшуюся границу. Без CHAT_ID ждём owner /start. Pending inbox обрабатывается до разрешения scheduler после рестарта.

Reply связывается только по точному ID известного prompt/entry, поздний reply сохраняет исходную дату периода. Голос/текст без reply свободны круглосуточно и в паузе. Если reply пришёл раньше результата send_message, entries хранят reply ID, затем sent_prompt одной транзакцией выполняет recursive reconciliation для прямых reply и дочерних дополнений, обновляя period/day/version. Никакого угадывания по часу. Unknown send без восстановленного ID остаётся unresolved в JSON, автоматического повторного send и ручного UI перепривязки пока нет.

## Голос

Metadata сохраняется сразу, загрузка в .part с ограничением 20 MiB, SHA256/fsync/atomic rename, затем ссылка в entries и stt job. Внешний STT разрешён пользователем; секреты предоставляет env, в БД и ZIP их нет. Groq endpoint `https://api.groq.com/openai/v1/audio/transcriptions`, модель whisper-large-v3-turbo, User-Agent. OpenAI adapter доступен конфигом, не требует смены кода. Native Telegram transcription/MTProto не используются.

STT ответ читается chunk-loop до EOF (лимит 2 MiB), не однократным read(n). Временные network/429/5xx повторяются с backoff до 5 попыток; 403 и невалидный ответ помечаются постоянной ошибкой, без автоматического платного повторения. download/STT ошибки не удаляют entry или аудио. Нет доступа — stt_unavailable. `/retry` — дополнительная служебная команда после устранения причины. `/correct` в reply на voice хранит исправление отдельно, обычный reply — дополнение. Позднее завершение STT обновляет запись и sync, но не шлёт неожиданный ночной push.

## Export по запросу

JSON schema_version=1 содержит диапазон/timezone, entries с ID/version, периодом/null, временами, исходным/текущим текстом, ревизиями, parent/reply IDs, аудио/transcript/correction/status. Не включаются file_id, секретные Telegram URLs и чужие данные. ZIP: diary.md, entries.json, audio/, manifest SHA256. Полный текст в JSON, Markdown безопасно экранируется без AI-пересказа. Отсутствующий audio даёт included=false и complete=false.

В Obsidian Markdown время отправки Telegram всегда показывается отдельно от времени события. Для явного числового времени в тексте или транскрипте (например, «в 13:30», «с 9 до 11») экспорт указывает время события; при его отсутствии пишет «Время события не указано». Транскрипт и его исправленная версия не меняются извлечением времени. Распознавание намеренно консервативно: время, произнесённое словами, не разбирается автоматически. Telegram Bot API не передаёт время начала записи voice; для такого случая доступно только время отправки сообщения.

`/export` без аргументов и кнопка «📦 Выгрузка» — только подсказка; `/today` отправляет ZIP за сегодня, `/export сегодня`/`вчера`/дата/диапазон — выбранный период до 31 дня и 45 MiB. Подпись ZIP сообщает даты и размер. При превышении лимита документ не отправляется. Экспорт не инициируется записью, STT, рестартом или backup.

Синхронизация записей в Obsidian выполняется вручную в два шага: кнопка «🗂 Синхронизировать с Obsidian»/`/sync` создаёт в SQLite одну `sync_requests` с состоянием requested, затем владелец запускает на компьютере батник «Синхронизировать дневник с Obsidian.bat», вызывающий `sync-poll`. Периодический опрос не используется. Без запроса SSH `sync-claim` возвращает idle без snapshot/download. При pending-команде claim→snapshot→scp→managed-block import→`sync-finish completed/error`; snapshot удаляется после завершения. Успешный локальный результат содержит `result=completed`; поле `result=ok` внутреннего импортёра не должно его перезаписывать. Повторы не создают дубль. `/status` показывает статус. `pull` сохраняется для ручного запуска, server/off-host backup не отключаются.

Sync реализован **полным восстанавливаемым snapshot**, а не первоначально предложенным per-entry outbox с удаляющим ACK. Это упрощает восстановление и не удаляет source data. Через root SSH `runuser -u hourlydiary -- installed-python -m hourly_diary snapshot …`, затем scp/SFTP. Обязательная установка пакета позволяет выполнять из любого cwd. Команды не содержат секретов; host key strict, BatchMode.

Windows проверяет exact ZIP set/duplicates/paths/links/bounds/checksums, пишет audio, затем только блок hourly-diary:start/end в Daily/YYYY-MM-DD.md. Сохраняет ручной текст снаружи, не создаёт дубли. Квитанция ID/version/day препятствует откату и позволяет удалить из старого дня запись, которую reconciliation перенёс в период. Конфликтные маркеры/конкурентные изменения останавливают import. File lock защищает второй импортёр; external editor/Yandex не участвуют в lock, поэтому узкая race остаётся — сохраняется pre-import копия. ACK только локальный, не гарантия облака.

Синхронизация не запускается по расписанию или при входе в Windows. Независимый off-host backup предусмотрен ежедневно в 04:15 и при входе, interactive session без хранения пароля. `deploy/register-windows-tasks.ps1` — устаревший установщик: он всё ещё регистрирует также sync каждые пять минут и при входе, поэтому для текущего ручного режима его запускать нельзя. Существующую задачу `HourlyDiary-ObsidianSync` следует оставить отключённой; расписание backup не заменяет ручную синхронизацию. Операционные сведения LIVE_STATUS относятся к дате соответствующей проверки.

## Backup и ограничения

Daily backup timer 03:45 Moscow: SQLite online backup/integrity, 14 поколений, неизменяемые аудио объекты по hash (одна копия на server backup root). Backup manifest сверяется при restore; target только новый каталог. Offhost pull копирует весь root с objects и проверяет restore каждого поколения до принятия новой копии. Env/secrets не входят в backup. Копия вне Яндекс Диска под приватным пользовательским каталогом; шифрование диска само приложение не делает.

Перед audio download общий DATA_DIR budget 500 MiB; при превышении capacity_error, без молчаливой чистки. Не является резервированием свободного места для всех будущих SQLite/ZIP/backup операций. Автоматического GC objects нет; нужна операторская проверка роста. При полном диске сохранение не подтверждается как успех.

Локально тестируются scheduler, owner filter, aiogram serialization/durable fake polling, replies/race/replay/edits/voice, STT chunks/error/retry, ZIP/sync/manual/stale/moved-day, backup restore, offhost pull и installed module вне cwd. Live-проверки не подменяются тестовыми записями в настоящем дневнике. Долгий простой VPS ограничен Telegram pending-update retention; удаление сообщения в Telegram не удаляет server copy. Реальные оставшиеся ограничения — в LIVE_STATUS.
