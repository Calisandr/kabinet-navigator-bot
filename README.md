# Кабинетный Навигатор

Telegram-бот для расписания компьютерных кабинетов из Google Таблицы.

Бот читает XLSX-экспорт таблицы, сам находит блоки по датам, уроки, ФИО/события и кабинеты. Данные кэшируются, чтобы не дергать Google при каждом сообщении.

## Что умеет

- Показывает расписание на сегодня и завтра.
- Показывает ближайшие даты из таблицы кнопками.
- Ищет учителя по фамилии или части ФИО.
- Показывает список всех учителей и событий.
- Обновляет данные командой `/refresh`.
- Принимает дату текстом: `02.05.2026`.

В исходной таблице нет отдельной колонки с предметом, поэтому бот показывает урок и кабинет. Если в таблицу добавят предмет отдельной колонкой, парсер можно расширить.

## Быстрый запуск

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

В файле `.env` укажи токен:

```env
TELEGRAM_BOT_TOKEN=123456789:your_token_here
GOOGLE_SHEET_ID=14inaoG-X5D6U3pLb1o0n3PwpZkSSlxGwoNlb6wVsU0Q
BOT_TIMEZONE=Asia/Krasnoyarsk
CACHE_MINUTES=15
WEBHOOK_SECRET=change_me_to_a_random_secret
WEBHOOK_URL=https://your-vercel-domain.vercel.app
```

Запуск:

```bash
python -m app.bot
```

Проверка чтения таблицы без запуска Telegram:

```bash
python -m scripts.check_schedule
```

## GitHub и хостинг

GitHub нужен как репозиторий для кода. Для круглосуточной работы бота нужен worker-хостинг: VPS, Render, Railway, Fly.io или похожий сервис.

Настоящий токен нельзя коммитить в GitHub. Добавляй его в переменные окружения/Secrets хостинга как `TELEGRAM_BOT_TOKEN`.

Проект уже содержит `Procfile`:

```text
worker: python -m app.bot
```

## Запуск на Vercel

Vercel запускает не постоянный worker, а HTTP-функции. Поэтому для Vercel используется webhook:

```text
https://your-vercel-domain.vercel.app/api/webhook
```

Шаги:

1. Импортируй GitHub-репозиторий в Vercel как новый проект.
2. В Vercel добавь Environment Variables:

```env
TELEGRAM_BOT_TOKEN=123456789:your_token_here
GOOGLE_SHEET_ID=14inaoG-X5D6U3pLb1o0n3PwpZkSSlxGwoNlb6wVsU0Q
BOT_TIMEZONE=Asia/Krasnoyarsk
CACHE_MINUTES=15
WEBHOOK_SECRET=любая_случайная_строка
```

3. Сделай Deploy.
4. После деплоя установи webhook из локальной папки проекта:

```bash
python -m scripts.set_webhook https://your-vercel-domain.vercel.app
```

Если потом захочешь снова запускать бота локально через polling, сначала удали webhook:

```bash
python -m scripts.delete_webhook
python -m app.bot
```

## Команды бота

- `/start` - открыть меню.
- `/today` - расписание на сегодня.
- `/tomorrow` - расписание на завтра.
- `/next` - ближайшие даты.
- `/date 02.05.2026` - расписание на дату.
- `/teacher Короткова` - поиск учителя.
- `/teachers` - список учителей и событий.
- `/refresh` - обновить таблицу.
