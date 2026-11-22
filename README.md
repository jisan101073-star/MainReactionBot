# JR Main Reaction Bot — Reliability Review Build

This build retains the existing Manager controls, worker registry, Firebase paths, and broadcast modes. It adds stricter lifecycle handling, reaction-permission classification, worker health alerts, and per-worker broadcast delivery diagnostics.

> **Important:** This is a source-level reliability build. It has not been deployed against the owner's live Telegram bots, Firebase database, or Render service. No release can honestly be described as 100% bug-free without those integration tests.

## Manager controls

- Add a worker, view registered workers, Start, Stop, Restart, Restart All, Stats, Broadcast, and Refresh remain available.
- Manager updates use `concurrent_updates(4)`. One broadcast does not occupy every update slot; a separate lock prevents two broadcasts from running at once. Conversation-state changes in the broadcast/add/home callbacks are committed before their first Telegram await to reduce concurrent-update interleaving.
- Per-worker lifecycle locks serialize Start/Stop/Restart/Remove, startup restoration, and Restart All. A second lock keyed by Telegram's actual bot ID prevents duplicate polling sessions even if malformed legacy registry keys refer to the same token. These locks coordinate one process only; multiple Render instances need an external distributed lock to coordinate safely. The code checks both `Application.running` and `Updater.running` before considering a worker active.
- A previous worker that did not stop cleanly blocks a replacement start. This is safer than knowingly creating two polling sessions for one token.
- Startup tries up to 3 times. A failed automatic startup keeps `enabled=True`, records `last_start_error`, and sends an owner alert naming the worker. Manual Start/Restart failures also preserve `enabled=True`; Stop is the explicit disable action.

## Worker health monitoring

- A background monitor checks the actual Application and Updater running flags and counts repeated polling errors. Silence in a quiet channel is not treated as proof of failure. `Stats` shows healthy Running and Degraded workers separately.
- Repeated polling errors mark the worker `degraded`; unexpected loss of its polling/application state marks it `stopped_unexpectedly`.
- When available, the Manager sends the owner the worker's username, Telegram ID, status, timestamp, and last error. The alert includes a Restart button and a My Bots shortcut.
- A later incoming update clears the degraded state and can trigger a recovery notification.
- Health alerts require the owner to have opened the Manager bot chat. Telegram may reject the notification if the owner has not started that bot or has blocked it.

## Reaction behaviour

- Each worker retains its own bot identity, own state and own reaction request semaphore. Requests within one worker are serialized; the workers are not placed into a shared cross-account scheduler.
- `get_chat()` reaction metadata is cached for 10 minutes. Telegram documents that an omitted `available_reactions` field means all standard emoji reactions are allowed. python-telegram-bot 22.x normalizes an omitted optional list to an empty tuple, so an empty tuple/list is treated as that default. PTB does not preserve enough information to distinguish omission from an explicitly empty list after parsing; Telegram remains the final permission check, and rejected reactions follow the normal error/pause path. Only standard emoji candidates supported by this bot are considered.
- If a candidate emoji receives a recoverable `BadRequest`, the worker tries another allowed candidate, up to 8 candidates for that message. If all sampled candidates fail, that chat's cached allow-list is invalidated and it gets a short 12-second cooldown; the next eligible message fetches fresh metadata.
- A confirmed chat-level permission/Forbidden error pauses that chat for 3 days and stores the pause under `reaction_bots/<bot_id>/reaction_chat_pauses`. An incoming `my_chat_member` update clears that pause and the cached reaction metadata so the chat can be re-evaluated sooner. Expired pauses are also re-evaluated on a later message.
- Telegram `RetryAfter` is treated as a worker-level rate limit. The worker stops retrying that message and applies Telegram's requested cooldown instead of trying more emojis while rate-limited.
- If reaction metadata cannot be read because of a temporary error or an unexpected data shape, a short-lived fallback is used and cached for 30 seconds; this is not treated as verified permission denial.

## Broadcast delivery and reporting

The existing three modes remain:

1. **Normal Message** — leaves the delivered message in the recipient's private chat.
2. **Auto Delete Message** — offers 1, 6, 12, or 24 hours and stores deletion jobs under `broadcast_delete_jobs`.
3. **Seen & Delete Message** — uses a recipient's explicit `I've Read` confirmation button. Telegram bots do not receive private-message read receipts.

- Every bot sends only through its own token and only to its own existing `reaction_bots/<bot_id>/users` list. User lists are deliberately not merged or deduplicated across bots.
- A person must have started each individual worker bot before that worker can initiate a private chat with them. Starting the Manager or a different worker does not subscribe that person to every worker. No code can legitimately bypass this Telegram limitation.
- The final report includes overall send attempts, actual failed attempts, deferred recipients, rate-limit events, plus a per-worker breakdown of saved recipients, attempted, sent, forbidden, not-reachable, other errors, rate limits, deferred recipients, malformed recipient keys, and delete/read-button errors. Deferred recipients are not counted as failed attempts because they were not sent yet. It does not expose recipient IDs in the Manager report.
- A bot-specific `RetryAfter` is honoured inline for up to 2 retries only when Telegram's requested wait is 10 seconds or less. A longer wait, or repeated short rate limits, causes the current and remaining recipients for that worker to be reported as deferred; other worker bots can still be processed without waiting for a long rate-limit period. There is no durable automatic queue for deferred broadcast recipients; the report tells the owner which bot and how many recipients need a later retry. The Manager report must not be interpreted as all recipients having received a copy when deferred or failed counts are non-zero.
- Duplicate Firebase user keys that normalize to the same positive Telegram chat ID are sent only once per worker; malformed and duplicate keys are counted as invalid entries.
- Ambiguous network failures are not blindly retried because Telegram may have accepted the message even if the response was lost; retrying could create duplicates.
- `BROADCAST_PRUNE_BLOCKED_USERS` remains **False**. User lists and Stats/User Count are not pruned or rewritten by default.
- A Manager-side lock prevents overlapping broadcast runs. Manager UI updates can still be processed while the broadcast is working.

## Firebase integrity and state saving

- A Firebase registry/state read error is not silently treated as an empty object. A malformed state collection is rejected so it is not replaced by an empty default.
- Normalization repairs malformed metric values but preserves unknown top-level and metric fields for forward compatibility.
- State saves use a per-worker lock and revision counter. Analytics flushes, full snapshots, and optional user cleanup coordinate through that lock.
- Broadcast reads each worker's saved user list but does not alter it unless the optional pruning switch is intentionally enabled.
- Runtime status is written with `update()` to the worker's registry record instead of replacing the entire record.
- Channel and group listing pages ignore malformed individual records instead of crashing while sorting or rendering the page. `/stats` reports the actual Application/Updater polling state rather than displaying a hard-coded healthy label; malformed uptime metadata safely displays zero.

## Shutdown behaviour and limitations

- SIGTERM/SIGINT enter one shared monotonic shutdown deadline (`SHUTDOWN_STOP_TIMEOUT`, default 20 seconds). Time is reserved for Manager shutdown after background-task and worker shutdown steps.
- Background monitor/scheduler/metrics/startup tasks, final analytics flush, workers, and Manager shutdown all use the same overall deadline.
- A final state snapshot is attempted only after polling/application shutdown and any older pending state save have completed. This avoids starting a newer full snapshot while an older write may still finish later.
- **A Python thread started by `asyncio.to_thread()` cannot be forcibly terminated safely.** If a Firebase SDK call is still in that thread at the deadline, the asyncio waiter is detached and shutdown continues; the thread may still finish in the process while it remains alive. This build avoids claiming that such a thread was killed and skips a competing final snapshot when a prior save is still pending.
- Therefore the 20-second value is an overall best-effort asyncio shutdown deadline, not a guarantee that Render can always exit within exactly 20 seconds under every OS/thread/network condition.

## Firebase paths and index

Existing paths are retained:

- `managed_bots/<bot_id>` — encrypted worker token and Manager registry information.
- `reaction_bots/<bot_id>/users` — recipient map for that specific worker.
- `reaction_bots/<bot_id>/...` — worker state, metrics and reaction permission pauses.
- `broadcast_delete_jobs/<bot_id>_<chat_id>_<message_id>` — timed/seen deletion work.

In Firebase Console → Realtime Database → Rules, merge this index into the existing rules; do not replace the rules document:

```json
{
  "rules": {
    "broadcast_delete_jobs": {
      ".indexOn": ["delete_at"]
    }
  }
}
```

If a `rules` object already exists, add `.indexOn` inside the existing `broadcast_delete_jobs` child without removing existing access rules. Without the index, due-job lookup falls back to a full read no more than once every 120 seconds; query retry is 600 seconds.

## Environment and deployment

Keep the existing Render environment variables:

- `MANAGER_BOT_TOKEN`
- `TOKEN_ENCRYPTION_SECRET`
- `FIREBASE_DATABASE_URL`
- `FIREBASE_SERVICE_ACCOUNT_JSON`
- `OWNER_ID` (optional; source default is retained)

Do not change `TOKEN_ENCRYPTION_SECRET` after worker tokens have been saved; the Manager needs the same secret to decrypt the registered tokens.

- Runtime: `python-3.11.9`
- Start command: `python bot.py`
- Install dependencies from `requirements.txt`.

## Verification boundary

Run locally with the bundled dependency-free tests:

```bash
python -m unittest discover -s tests -v
python -m py_compile bot.py
```

These are isolated tests of selected real source functions and syntax compilation using the local interpreter. They do not verify actual PTB/Firebase imports under Python 3.11, Telegram permissions, live polling, real Firebase rules/indexes, Render signal timing, or end-to-end broadcast delivery. Verify those after deployment before relying on the service.

## Group Manager (owner only)

Open the Manager Bot and press **🗂 GROUP MANAGER**.

1. Pick a Reaction Bot (🟢 running, 🟡 enabled but not running, ⚪ disabled).
2. Pick one of the groups that bot is in (groups and supergroups only; channels are not listed).
3. Press **✉️ Send Message**, type your message, and check the preview.
4. Choose when it should be deleted: **Permanent**, 1, 6, 12, 24 or 47 hours, or press **✏️ Custom hours** and type your own time (for example `2`, `5.5` or `47`; Bangla digits work too). The shortest time is 1 minute and the longest is **47 hours**. Anything above 47 hours is refused.
5. Press **✅ Send now**. The Reaction Bot posts the message in that group with a permanent **➕ Add Bot to Group** button under it.

Things to know:

- **Files:** all Group Manager logic is in `group_manager.py` (keep it next to `bot.py`). `bot.py` only imports it, adds one menu button and registers it on the Manager Bot. If `group_manager.py` is missing or broken, the Manager Bot starts normally without the button.
- **The list is read, never written.** Groups come from each Worker's own saved state (`reaction_bots/<bot_id>/groups`, or the running Worker's memory), so a group appears as soon as the bot is added by anyone and disappears when the bot is removed. Group Manager cannot change any Worker or Firebase data except the delete job below.
- **Delete timer:** a timed message gets a job in `broadcast_delete_jobs` in the same format the existing delete scheduler already processes, so no second scheduler runs. Telegram only allows a bot to delete its own message for 48 hours after it was sent, so no delete time above 47 hours is ever accepted, whether chosen with a button or typed. "Permanent" creates no job.
- **Reactions are untouched.** A Worker keeps reacting in every group exactly as before: where Telegram allows it, it reacts; where it does not (no permission, reactions off), the existing pause logic stops it.
- **If a message cannot be sent** (group does not let members send, bot removed or muted, Telegram rate limit) the bot says so plainly and nothing is saved.
- **Add Bot to Group button:** uses the same link as the Worker's own "ADD TO GROUP" button (`https://t.me/<bot>?startgroup=true`). To ask for admin rights through the link, set `ADD_BOT_ADMIN_RIGHTS` at the top of `group_manager.py` (for example `"delete_messages"`); the group's admin still confirms them. The Worker does not need admin rights to post.
- Telegram limits: at most 20 messages per minute in one group and about one per second per chat. Writing messages by hand is far below that. Posting repeated promotional messages in groups you do not run can lead to spam reports, which can limit the bot.
