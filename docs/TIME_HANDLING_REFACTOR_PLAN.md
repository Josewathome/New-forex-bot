# Time-Handling Refactor Plan — New-forex-bot

**Status:** Planning document only. No code has been changed yet.
**Read first:** `../../TIME_HANDLING_REFACTOR_OVERVIEW.md` (shared decision: MT5 server time is the sole authoritative clock; no true-UTC alignment; exactly one conversion boundary survives, for external news timestamps).

## The good news for this bot specifically

New-forex-bot's `BrokerClock` (`src/infrastructure/market_data_repo.py:20-135`) was already built directly from MT5 tick time, with a sensible cache + monotonic-delta dead-reckoning fallback and no NTP layer at all. Under the old "must match true calendar UTC" framing, this looked like the bot's biggest liability (its docstring claims tick time is "already in UTC," which a sibling file's own `detect_utc_offset()` docstring contradicts). **Under the new model, that contradiction is moot** — this bot doesn't need to know or care whether MT5 tick time equals true UTC, because MT5 tick time *is* the target, not a stand-in for something else. `BrokerClock` needs no structural redesign. It becomes the reference implementation the other two bots' rewritten `BrokerClock` are modeled on.

What actually needs fixing here is narrower than the original audit implied: one real bug (`trade_repo.py`'s offset subtraction, now clearly wrong under the new model), plus the same "wire the clock into everything that bypasses it" pass the sibling bots need.

---

## 1. Remove the now-unnecessary offset subtraction on position timestamps

- `trade_repo.py:99-100`:
  ```python
  open_dt = datetime.fromtimestamp(p["open_time"], tz=timezone.utc) \
            - timedelta(hours=getattr(config, "BROKER_UTC_OFFSET_HOURS", 0))
  ```
  `p["open_time"]` is already raw MT5 epoch time (`mt5_gateway.get_open_positions()`, `int(p.time)`) — exactly the same basis as every other MT5 timestamp this bot uses (candles, ticks, `BrokerClock.now()`). Under the new model, this needs **zero** conversion. **Fix: delete the subtraction.** Use `datetime.fromtimestamp(p["open_time"], tz=timezone.utc)` as-is — the `tz=timezone.utc` is just a Python API label for "this is an aware datetime," not a claim about calendar UTC.
  - This directly fixes the corrupted `Trade.created_at` for MT5-recovered positions and the resulting early time-based exits (`execution_service.py:1051-1056`, `MAX_TRADE_DURATION_MINUTES`).

## 2. Stop treating `detect_utc_offset()` as load-bearing for internal time

- `mt5_gateway.py:301-335` `detect_utc_offset()` computes the broker offset against `time.time()` (host clock) and is currently consumed only by the subtraction removed in §1. Once that's gone, this function has no remaining internal-time consumer.
- Keep it only if the news-ingestion boundary (§3) needs a static offset constant — in that case, rename it conceptually to a one-time verification tool, not a runtime auto-detector: verify the value once against the live MT5 terminal, hardcode it as `MT5_NEWS_OFFSET_HOURS`, and delete the runtime call from `main.py:307-308`/`main_stream.py:141-144`'s startup path.

## 3. News ingestion: the one place a conversion still happens

- Check `mt5_news_client.py`, `news_client.py`, and `composite_news_client.py` in this repo (same file names as the sibling bots — confirm they exist and mirror the same pattern). Apply the same fix as the other two bots: normalize every external news source (ForexFactory, MyFxBook, and MT5's own calendar API if it turns out to use a different basis than tick time — verify once) onto MT5-server-time basis at ingestion, using the single static `MT5_NEWS_OFFSET_HOURS`, before any no-trade-window construction.

## 4. Session windows: verify they're expressed in MT5-server-time terms

- `config.py:416-419`: `TRADING_SESSIONS = {"LONDON": {"start": 8, "end": 16}, "NEW_YORK": {"start": 13, "end": 21}}`. These read like true-UTC hours (London open 08:00 UTC, NY open 13:00 UTC). Under the new model, they need to be expressed in MT5-server-time hours instead — since `BrokerClock.now()` already returns raw MT5 time with no offset applied, and `is_in_trading_session()` (`analysis.py:77-98`) compares directly against `dt.hour`.
- **Action:** verify the current relationship between MT5 server time and real session opens (live, against the terminal), and shift these two config values by that amount if they're currently defined in true-UTC terms. Document the verification date/method in a comment next to the constant.
- While touching this function, add a defensive assertion that `dt.tzinfo` is set (currently no guard exists — `analysis.py:77-98` trusts the caller silently).

## 5. Wire the single clock into everything that currently bypasses it

- `application/scheduler.py:74` — `Scheduler` has no clock parameter at all (`main.py:323`, `main_stream.py:211` both instantiate it bare). Add one; use it in `_loop()` instead of `datetime.now(tz=timezone.utc)`. This is what governs the Friday 21:00 "market close" zone/cache wipe and the Saturday weekly-backtest trigger — both inherently MT5/broker-time concepts that should use MT5 time regardless of the true-UTC simplification.
- `application/weekly_backtest_runner.py:51-54,232` and `main.py:598-608` (`run_backtest()` CLI default date range) — both bootstrap their date range from `datetime.now(tz=timezone.utc)` with no access to `BrokerClock` at all. Give both a `clock` reference and use `clock.now()`.
- `application/news_manager.py:29-30,35-36` (`refresh_if_needed`, `is_blocked`) — remove the `now or datetime.now(tz=timezone.utc)` fallback; make `now` required.
- `application/execution_service.py:173-181` and `application/gap_recovery_service.py:115-119` (`_now()` helpers) — same fix: remove the "fall back to host clock if no clock injected" branch. If a caller genuinely has no clock (e.g. a test), that should be an explicit, visible test double, not a silent runtime fallback.
- `application/diagnostics.py:57,177` — host-clock report timestamps; low priority, optionally route through the shared clock for consistency.

## 6. Housekeeping: make the clock discoverable

- `BrokerClock` currently lives inline inside `market_data_repo.py`, a file whose name gives no hint that the bot's single source of truth for "now" is defined there. **Move it** to a dedicated module (e.g. `infrastructure/mt5_clock.py`), matching the naming convention the sibling bots use for their (now-rewritten) clock module. This directly prevents the exact failure mode the original audit flagged: a future contributor grepping for "clock" or "time" utilities before wiring `Scheduler`/`WeeklyBacktestRunner` would otherwise find nothing.

## 7. Cosmetic / non-trading-critical

- `api/server.py:527-532` — `"server_time"` field is host clock despite the name; relabel or route through the shared clock now that it's centralized in its own module.
- `trade_journal.py`, `zone_store.py`, `cache_store.py`, `cleanup_service.py`, `checkpoint_service.py`'s fallback branch — host clock acceptable for logging/retention; optional to route through the shared clock for full consistency.
- `mt5_gateway.py:41-44` (`_mt5_log_has_invalid_account`'s host-date log-file lookup) — self-consistent, dead/unused helper; no action needed unless it's actually called somewhere.

## Verification checklist after implementing

- [ ] `grep -rn "BROKER_UTC_OFFSET_HOURS" src/` → no hits in `trade_repo.py` (subtraction removed); only the renamed news-ingestion constant remains, if needed.
- [ ] `grep -rn "datetime.now(tz=timezone.utc)\|time.time()" src/` → only hits remaining are non-critical logging/dashboard/JWT, and any explicit test doubles.
- [ ] `Scheduler`, `WeeklyBacktestRunner`, and `run_backtest()`'s CLI default all take/use a `clock` reference.
- [ ] `BrokerClock` lives in a discoverably-named module.
- [ ] `TRADING_SESSIONS` config values are documented as MT5-server-time hours, with the verification method/date noted.
- [ ] Every function gating a trade (session, news, entry, cooldown, exit) takes `now`/`clock` as a required parameter — no silent host-clock fallback left on `NewsManager`, `ExecutionService._now()`, or `GapRecoveryService._now()`.
