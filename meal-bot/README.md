# meal-bot

A Telegram bot that turns a photo of your plate into a logged calorie + protein
estimate. Photograph the meal, the bot runs it through a Claude vision prompt,
and it stores a row in a local SQLite database and replies with the estimate.

Why a bot and not an app: logging has to take five seconds or you quit. Adherence
beats accuracy — a rough number you actually log every day beats a precise one
you abandon in week two.

## What it does

- Photo or text → itemized estimate (kcal range, protein/carbs/fat, confidence)
- Reply to correct it ("actually 300g", "the other shake was just water") — it
  re-estimates, and it can tell a correction from a new snack
- `/today` — running total for the day
- `remember <name>: <facts>` — teach it your staple foods so it uses label data
  instead of guessing
- Optional HTTP endpoint that ingests weight/steps/sleep from the iOS
  **Health Auto Export** app, so your Garmin scale data lands in the same database

## Setup

Requires Node 22+ (for the built-in `node:sqlite`) and the
[Claude Code CLI](https://claude.com/claude-code) on your PATH.

1. Create a bot with [@BotFather](https://t.me/botfather) and copy the token.
2. `cp ../.env.example .env` and fill in `TELEGRAM_BOT_TOKEN`.
3. `node bot.mjs`
4. Message your bot once — the first chat to talk to it becomes the owner; every
   other chat is ignored.

Run it as a service (systemd, pm2, a screen session) so it stays up.

## The frozen-prompt rule

The vision prompt and model are deliberately **fixed for the length of a cut**.
Photo calorie estimates carry a consistent bias; a weekly TDEE recalculation
(compare your calorie logs against your actual weight trend) absorbs a *steady*
bias but not a *drifting* one. Change the prompt mid-cut and you lose your
calibration. Freeze it, and let the math correct for it.

## Privacy

Everything is local: the SQLite file, the meal photos, your keys. Nothing leaves
your machine except the vision call to the Claude API. `data/` and `.env` are
git-ignored — keep them that way.
