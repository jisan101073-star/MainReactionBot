# Source Verification Report

## Input and scope

- Reviewed the user-uploaded `JR_MainReactionBot_Reviewed_Final.zip` in this conversation.
- Kept the original project layout and environment/runtime configuration.
- Updated `bot.py`, `README.md`, and added `tests/test_reliability.py`.

## Changes applied

- Corrected the undefined `_is_fatal_reaction_error` reference used by private-message fallback reactions.
- Private fallback reaction `RetryAfter` now creates a worker-level cooldown instead of sleeping/retrying inside the message handler.
- Fixed the `available_reactions` default handling: PTB 22.x converts an omitted optional list to `()`, so empty tuple/list values are treated as Telegram's documented default (all standard emoji). PTB does not preserve enough information to distinguish omitted from explicit-empty after parsing; Telegram remains the final permission check. Existing recoverable-error, cache invalidation and chat-pause handling remains in place.
- Worker health monitor is started by `main`; it tracks actual Application/Updater state and repeated polling errors, persists runtime state, and sends owner alerts identifying the worker.
- Added per-worker lifecycle locks across automatic startup, Start/Stop/Restart/Remove and Restart All, plus an actual Telegram bot-ID startup lock to prevent duplicate pollers from malformed/duplicate registry records in the same process. A previous session that fails to stop prevents starting a duplicate session.
- Automatic startup failure now keeps `enabled=True`, stores the error and sends an alert instead of silently disabling the worker.
- Shutdown uses one shared deadline across background tasks, final metrics flush, workers and Manager. It reserves time for Manager shutdown and does not claim that a blocking OS thread can be force-killed.
- Broadcast sends now produce overall and per-worker recipient outcome counts. Deferred recipients are tracked separately from actual failed attempts. Telegram `RetryAfter` is retried inline only for waits up to 10 seconds; longer/repeated rate limits cause remaining recipients for that worker to be reported as deferred so other workers can still be processed. Duplicate normalized chat IDs are not messaged twice. The report explains per-worker opt-in limitations.
- Manager add/broadcast/home/delay callbacks now commit their conversation-state transitions before the first Telegram await, reducing races with concurrent Manager updates.
- Channel/group listing pages skip malformed individual Firebase child records instead of crashing. `/stats` reports current Application/Updater polling state rather than a hard-coded healthy message; uptime formatting safely handles malformed or future timestamps.
- Added a Manager-side broadcast lock and preserved `concurrent_updates(4)`.
- Updated docs to remove stale/incorrect claims about startup disable behavior, empty reaction lists, retry timing and shutdown guarantees.

## Tests executed in this environment

- `python -m py_compile bot.py` — passed.
- `python -m unittest discover -s tests -v` — 18 isolated tests passed.
- ZIP integrity will be checked after packaging.

The tests compile actual top-level functions from `bot.py` with test doubles. They cover PTB-style empty reaction metadata defaults, restricted reaction lists, alternate emoji handling, rejected-candidate cache invalidation, chat permission pauses, `RetryAfter` cooldown, malformed channel/group child records, safe uptime formatting, broadcast delivery counts/defer behavior, duplicate/malformed recipient keys, malformed Firebase state rejection, Firebase state normalization, same-identity duplicate worker startup prevention, worker running-state checks, and shutdown deadline handling.

## Not tested

- Actual Python 3.11.9 runtime import and startup with all installed dependencies. The container interpreter is Python 3.13.5 and the Telegram/Firebase packages are not installed here.
- Live Telegram API calls, permission changes, worker polling conflicts, owner notifications, and actual broadcast recipients.
- Live Firebase data/rules/indexes, Render deploy or SIGTERM behavior.
- Multi-process deployment. The lifecycle locks are in-process only; they do not coordinate separate Render instances.

No claim of 100% defect-free behaviour or live delivery is made.

## Group Manager (added on top of the reviewed code)

- New file `group_manager.py`; `bot.py` changed in three places only (optional import, one menu button shown only after successful registration, one `register()` call wrapped in try/except). Nothing else in `bot.py` was touched.
- `tests/test_group_manager.py` (46 tests, no Telegram needed; 61 tests in total with the existing ones): owner-only access, read-only group lists (memory for running bots, `reaction_bots/<id>/groups` for stopped bots, never `channels`), HTML escaping, pagination, callback-data length, preview then timer then confirm flow, one delete job per timed message and none for permanent, double-tap protection, timers below 48 hours, temporary Bot opened and closed for stopped workers, permission/removed/rate-limit errors reported without saving a job, honest report when the delete job cannot be saved, text handler passes through when no compose session is open, session cleared by other Manager actions, a failed preparation leaves the Manager untouched, typed delete times (decimals, comma, `2h`, Bangla digits; 47 hours accepted, 47.01 and above refused, below 1 minute refused, garbage and huge numbers refused), the send step itself refusing any time outside 1 minute to 47 hours before anything is posted, empty/whitespace messages answered instead of leaking to the Manager, message length measured in UTF-16 units like Telegram, the page number kept for Back/Cancel/Send-another buttons, the Send button acknowledged before slow work, and an owner message when a failure happens after the button was already acknowledged.
- Wiring checks with a fake Application: handlers go in groups -2/-1 only, all 11 original Manager handlers and all original menu buttons remain, a failing `register()` or a missing `group_manager.py` leaves the Manager running without the button.
- Later change: `bot.py` gained `REPLY_TO_START_IN_GROUPS = False` plus a 5-line early return in `start_command` (no welcome menu in groups, private chats unchanged; `tests/test_start_in_groups.py`), and the Add Bot to Group link now carries `admin=<rights>` (`ADD_BOT_ADMIN_RIGHTS`, 67 tests in total). The right names in the link follow the Bot API names without `can_`; they were not tried against Telegram.
- Not done: no live run against Telegram, Firebase or Render. The Add Bot to Group deep link, `<blockquote>` formatting and handler-group ordering follow python-telegram-bot and Telegram behaviour but were not exercised against the real services.
