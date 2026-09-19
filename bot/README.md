# Bot

The Basic tier's entry point (PRD 4.1): Telegram messages in, Telegram messages
out, with a lesson walked one step at a time.

```bash
make run     # the API, in one terminal
make bot     # this, in another
```

Needs `TELEGRAM_BOT_TOKEN` in `.env` — from BotFather — plus a running API at
`BOT_API_BASE_URL` and the Redis from `docker compose up -d`.

## How it is put together

| file | what it holds |
|---|---|
| `flow.py` | the lesson state machine. Pure: no Telegram, no HTTP, no clock. This is where the conversation's behaviour lives and where it is tested. |
| `handlers.py` | `Conversation` takes plain values (a learner id, some audio bytes) so a whole lesson can be played through without Telegram; the functions below it are the python-telegram-bot adapters. |
| `api_client.py` | the only way this process reaches the system. HTTP, never an import of the services. |
| `session.py` | where each learner is, in Redis, with a day's expiry. |
| `i18n.py` | the message catalogue. No sentence is written in Python. |
| `main.py` | wiring and `run_polling()`. |

## Two rules it exists to obey

**It decides nothing.** Whether an attempt passed, whether there is quota left,
which recording to play — every one of those comes back from the API
(ARCHITECTURE section 1). The one number it reads from configuration is
`SCORING_MAX_RETRY`, and it reads the same key the backend does rather than
inventing a second one.

**It says nothing of its own.** Every string is a key in `locales/`
(CLAUDE.md section 7). `km.json` and `zh.json` are placeholders today and are
meant to look wrong until a native speaker writes them.

## Polling, not webhooks

PRD section 8 specifies webhook mode, which needs a public HTTPS endpoint this
project does not have yet. The switch is one call in `main.py`
(docs/DECISIONS.md D-049) and is a deployment task, not a code change.
