# JR Reaction Bot Manager — Broadcast Update

This archive is based on the supplied `MainReactionBot-main.zip`. The existing Reaction Bot behavior, user registration, statistics, and Firebase user-count structure are retained.

## Broadcast menu

The Manager Bot now has a **📢 BROADCAST** button with three modes:

1. **Normal Message** — leaves the sent message in each recipient's private chat.
2. **Auto Delete Message** — offers 1, 6, 12, or 24 hours. Deletion jobs are stored under the separate Firebase path `broadcast_delete_jobs`, so a Render restart does not lose the scheduled delete time.
3. **Seen & Delete Message** — adds an **I've Read** link button. Telegram bots do not receive read receipts, so recipients confirm manually. The link opens the Manager Bot; it records the confirmation and schedules deletion one minute later. The Manager Bot must be reachable, and a recipient who has never started it may need to press **Start** after opening the link.

## Delivery rules

- The Manager reads each bot's existing `reaction_bots/<bot_id>/users` Firebase map.
- Every registered bot sends independently from its own token to its own user list.
- User lists are not merged or deduplicated. If the same person has started 30 different registered bots, they can receive 30 separate copies, one from each bot.
- Registered bots can send even when their polling worker is stopped, because the Manager temporarily initializes that bot's token for sending.
- Bots removed from the Manager registry are not included.
- A delivery can fail if a user has blocked that particular bot, never started that bot, or the bot token is invalid.
- No new user counter is introduced, and the existing `users` count / Stats data is not changed by broadcasting.

## Firebase paths

Existing paths remain unchanged:

- `managed_bots/<bot_id>` — encrypted bot token and Manager registry information.
- `reaction_bots/<bot_id>/users` — the existing per-bot recipient list.
- `reaction_bots/<bot_id>/...` — the existing per-bot state and analytics.

New path used only for message deletion scheduling:

- `broadcast_delete_jobs/<bot_id>_<chat_id>_<message_id>`

## Environment variables

Keep the existing Render environment variables:

- `MANAGER_BOT_TOKEN`
- `TOKEN_ENCRYPTION_SECRET`
- `FIREBASE_DATABASE_URL`
- `FIREBASE_SERVICE_ACCOUNT_JSON`

Do not change `TOKEN_ENCRYPTION_SECRET` after tokens are stored; the Manager needs the same secret to decrypt each registered worker token.

## Deploy

- Runtime: `python-3.11.9` (from `runtime.txt`)
- Start command: `python bot.py`
- Dependencies: `requirements.txt`
