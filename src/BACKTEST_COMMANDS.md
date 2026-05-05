# Forex Bot Backtest Commands Reference

All commands use this Docker template:
```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start YYYY-MM-DD --end YYYY-MM-DD --name report_label"
```

---

## 1. Baseline (Already Run)

| Period | Notes |
|--------|-------|
| Nov 2024 – Apr 2025 | 6-month baseline showing +765% — this is your current reference |

```bash
# Already completed — results in src/backtest_results/
# Win rate: 64.7%, 30k → 259k KES
```

---

## 2. Full Year Coverage (6-Month Splits)

Run these back-to-back to get a complete picture of any 12-month window.

### 2a. Summer Chop + Range Markets (May–Oct 2024)
- **Why it matters**: Northern-hemisphere summer = reduced institutional participation, tight ranges, fake breakouts, stop hunts. Your OBs will look valid but lack follow-through.
- **Most affected pairs**: EURUSD, GBPUSD, USDJPY
- **ATR character**: Low–Normal (summer compression → sudden spikes)

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2024-05-01 --end 2024-10-31 --name summer_chop_2024"
```

### 2b. Recent Volatility (Nov 2024 – Apr 2025)
- Already run as baseline. Re-run with new story-reading code after all fixes applied:

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2024-11-01 --end 2025-04-30 --name baseline_with_story_fixes"
```

### 2c. Pre-Baseline Context (Nov 2023 – Apr 2024)
- **Why it matters**: Covers the October 2023 Hamas shock, BoJ rate decision chaos, and USD strength trend.
- **ATR character**: High

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2023-11-01 --end 2024-04-30 --name pre_baseline_2023_2024"
```

### 2d. Summer Chop (May–Oct 2023)
- **Why it matters**: BoJ YCC surprise (July 2023) sits inside this window. Tests bot behaviour when a central bank kills the trend mid-range.
- **ATR character**: Normal → Extreme spike (BoJ)

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2023-05-01 --end 2023-10-31 --name summer_chop_plus_boj_2023"
```

---

## 3. Trump / Trade War Events

### 3a. Liberation Day Tariff Shock (Apr–May 2025)
- **Why it matters**: April 2 "Liberation Day" — blanket 10–25% tariffs on all imports. DXY dropped, gold +5% overnight. USD pairs whipsawed 200–400 pips.
- **Most affected pairs**: USDJPY, USDCNH, EURUSD, USDCAD
- **ATR character**: Extreme (2–4× normal ATR for 2 weeks)

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2025-04-01 --end 2025-05-31 --name trump_liberation_day_2025"
```

### 3b. Trade War Phase 1 (Aug–Dec 2019)
- **Why it matters**: US-China tariff escalation, yield curve inversion, Fed cut cycle. Classic "narrative whipsaw" — every tweet moved markets 80–150 pips.
- **Most affected pairs**: USDJPY (safe haven flows), USDCNH, AUDUSD (China proxy)
- **ATR character**: High with intraday spikes

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2019-08-01 --end 2019-12-31 --name trump_trade_war_2019"
```

### 3c. Trade War Peak Tension (May–Aug 2019)
- **Why it matters**: May 10 tariff hike from 10% to 25% on $200B Chinese goods. Immediate 200-pip AUDUSD drop. Tests bot's zone-story reading under news-driven distribution.
- **ATR character**: Extreme (first 2 weeks), then High

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2019-05-01 --end 2019-08-31 --name trump_tariff_hike_2019"
```

---

## 4. Iran / Middle East Geopolitical Events

### 4a. Soleimani Assassination & Retaliation (Jan 2020)
- **Why it matters**: January 3 — US kills Qasem Soleimani. Oil +4%, safe havens surged. Markets feared full war. Reversed within 5 days when Iran de-escalated. Tests bot on "spike and reverse" patterns.
- **Most affected pairs**: USDJPY (down 150 pips, safe haven bid), USDCAD (oil-linked), XAUUSD
- **ATR character**: Extreme for 72 hours, then Normal

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2020-01-01 --end 2020-02-15 --name iran_soleimani_jan_2020"
```

### 4b. Iran-Israel Direct Exchange (Apr 2024)
- **Why it matters**: April 13–14 — Iran launched 300+ drones/missiles at Israel (first direct attack in history). USDJPY dropped 200 pips in hours. Tests extreme gap and OB invalidation behaviour.
- **Most affected pairs**: USDJPY, USDILS (NDF), gold, oil
- **ATR character**: Extreme (weekend gap + Monday open chaos)

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2024-04-01 --end 2024-05-15 --name iran_israel_attack_apr_2024"
```

### 4c. Israel Counterattack on Iran (Oct 2024)
- **Why it matters**: October 26 — Israel struck Iranian military targets directly. Second spike in the same year. Tests whether your bot re-enters after first spike was mapped.
- **ATR character**: High (markets partially priced in escalation)

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2024-10-01 --end 2024-11-30 --name iran_israel_counter_oct_2024"
```

### 4d. Iran Nuclear Escalation (Jun–Jul 2025)
- **Why it matters**: June 2025 — US bombed Iran nuclear sites. One of the biggest Middle East escalations since 2003. Tests bot on ultra-extreme ATR environment with sustained oil and safe-haven disruption.
- **Most affected pairs**: USDJPY, USDCAD, XAUUSD, EURUSD
- **ATR character**: Extreme (sustained, not spike-and-reverse)

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2025-06-01 --end 2025-07-31 --name iran_nuclear_us_strike_2025"
```

---

## 5. COVID-19 Market Crash

### 5a. Initial Crash Phase (Feb–Apr 2020)
- **Why it matters**: The fastest 30% equity drawdown in history. DXY surged (liquidity demand), then Fed emergency cuts. All correlations broke. Your H1 zones were mapped into a different world — tests whether story-reading catches distribution before zones fail.
- **Most affected pairs**: ALL pairs — USDJPY +700 pips then -700 pips, GBPUSD -1500 pips
- **ATR character**: Extreme (5–10× normal ATR, sustained 6 weeks)

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2020-02-01 --end 2020-04-30 --name covid_crash_q1_2020"
```

### 5b. Recovery Phase (May–Oct 2020)
- **Why it matters**: V-shaped recovery in FX (not equities). Trending markets with clean OBs. Tests the bot's best-case scenario — high ATR, clear institutional footprints.
- **ATR character**: High → Normal (trending, then stabilising)

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2020-05-01 --end 2020-10-31 --name covid_recovery_2020"
```

---

## 6. Central Bank Shocks

### 6a. BoJ YCC Policy Shift (Jul–Sep 2023)
- **Why it matters**: July 28 — Bank of Japan quietly widened its yield curve control band (0.5% → 1.0%). USDJPY dropped 300 pips in one session. Classic "mapped OB gets blown through" scenario — tests M5 distribution detection.
- **Most affected pairs**: USDJPY (primary), EURJPY, GBPJPY (all JPY crosses)
- **ATR character**: Extreme for 48h, then High for 2 weeks

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2023-07-01 --end 2023-09-30 --name boj_ycc_shift_2023"
```

### 6b. BoJ Rate Hike & Yen Carry Unwind (Jul–Aug 2024)
- **Why it matters**: July 31 — BoJ raised rates unexpectedly. Yen carry trade (borrow JPY, buy everything else) unwound in days. USDJPY fell 1500 pips in 3 weeks. One of the largest single-currency moves in 20 years.
- **Most affected pairs**: ALL JPY crosses — USDJPY, EURJPY, GBPJPY, AUDJPY
- **ATR character**: Extreme (2024 peak volatility event)

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2024-07-15 --end 2024-09-15 --name boj_rate_hike_carry_unwind_2024"
```

### 6c. EUR/USD Parity Break (Jul–Sep 2022)
- **Why it matters**: July 14 2022 — EUR/USD hit 0.9953 (below parity for first time since 2002). ECB hiking into recession, energy crisis. Pure trend with no pullbacks — tests bot's ability to trail runners.
- **Most affected pairs**: EURUSD, EURGBP
- **ATR character**: High (sustained directional trend)

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2022-07-01 --end 2022-09-30 --name eur_parity_break_2022"
```

### 6d. SVB / Credit Suisse Banking Crisis (Mar 2023)
- **Why it matters**: March 10 — Silicon Valley Bank collapsed. March 19 — Credit Suisse forced merger. USD whipsawed as markets repriced Fed rate path. Tests bot on "fundamental narrative reversal" where OBs form, then the driver disappears.
- **Most affected pairs**: EURUSD, USDCHF (CHF safe haven flows), USDJPY
- **ATR character**: Extreme for 5 days, then rapidly normalising

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2023-03-01 --end 2023-04-15 --name svb_credit_suisse_march_2023"
```

---

## 7. Political Shocks

### 7a. Russia-Ukraine Invasion (Feb–Apr 2022)
- **Why it matters**: February 24 — Russia invades Ukraine. EUR sold (European war risk), CHF and JPY bid (safe havens), commodity currencies surged (AUD, CAD, NZD — food/energy). Tests bot on cross-asset correlation breakdown. Your H1 OBs on EURUSD became worthless in 48 hours.
- **Most affected pairs**: EURUSD (massive), USDCHF, USDCAD, USDRUB (extreme)
- **ATR character**: Extreme (first 2 weeks), sustained High for 2 months

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2022-02-15 --end 2022-04-30 --name russia_ukraine_invasion_2022"
```

### 7b. Liz Truss Mini-Budget Crash (Sep–Oct 2022)
- **Why it matters**: September 23 — UK mini-budget with unfunded tax cuts. GBP/USD dropped 500 pips to record low 1.0350 in 5 days. BoE emergency bond-buying. The fastest single-currency crash in G7 history. Tests total zone invalidation.
- **Most affected pairs**: GBPUSD (primary), GBPJPY, EURGBP
- **ATR character**: Extreme (GBP-specific, 5–8× normal ATR)

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2022-09-15 --end 2022-10-31 --name liz_truss_gbp_crash_2022"
```

### 7c. Hamas Attack & Middle East Escalation (Oct–Nov 2023)
- **Why it matters**: October 7 — Hamas attack on Israel. Oil surged, safe havens bid. Markets oscillated on escalation/de-escalation headlines for weeks. Tests bot in "headline-driven, no-trend" environment.
- **Most affected pairs**: USDJPY, USDCAD (oil), XAUUSD
- **ATR character**: High with periodic Extreme spikes

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2023-10-01 --end 2023-11-30 --name hamas_attack_oct_2023"
```

### 7d. Brexit Referendum Shock (Jun–Aug 2016)
- **Why it matters**: June 23 2016 — UK votes Leave. GBP dropped 1800 pips overnight. The single largest overnight move in a G7 currency in modern history. Tests the absolute limits of your zone system.
- **Most affected pairs**: GBPUSD (extreme), EURGBP, GBPJPY
- **ATR character**: Extreme (off-the-charts — 10× normal ATR for 1 session)

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2016-06-20 --end 2016-08-31 --name brexit_referendum_shock_2016"
```

---

## 8. Seasonal & Normal Market Conditions

### 8a. Summer Chop (Jun–Aug) — Annual Pattern

Low-volatility range markets. Best for testing whether the bot generates false signals on tight, fake breakouts.

```bash
# Summer 2024
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2024-06-01 --end 2024-08-31 --name summer_chop_jun_aug_2024"

# Summer 2023
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2023-06-01 --end 2023-08-31 --name summer_chop_jun_aug_2023"

# Summer 2022
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2022-06-01 --end 2022-08-31 --name summer_chop_jun_aug_2022"
```

### 8b. Year-End Thin Liquidity (Dec–Jan)

Low volume, erratic moves, institutional books closed. Tests bot behaviour when spreads widen and stop-hunts dominate.

```bash
# Dec 2024 – Jan 2025
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2024-12-01 --end 2025-01-31 --name year_end_thin_2024_2025"

# Dec 2023 – Jan 2024
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2023-12-01 --end 2024-01-31 --name year_end_thin_2023_2024"
```

### 8c. Post-NFP Weeks (High-Normal Conditions)

Non-Farm Payrolls fire the first Friday of each month. The following 1–2 weeks show clean directional moves as institutions absorb the data. Best scenario for your OB + story system.

```bash
# Q1 2025 — post-NFP trending
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2025-01-06 --end 2025-03-31 --name post_nfp_trending_q1_2025"

# Q3 2024 — post-NFP trending
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2024-07-08 --end 2024-09-27 --name post_nfp_trending_q3_2024"
```

### 8d. Normal Trending Quarter (No Major Events)

Clean trending markets with proper institutional structure — this is what your bot was designed for.

```bash
# Jan–Mar 2024 (USD strength trend, clean H1 OBs)
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2024-01-08 --end 2024-03-28 --name normal_trending_q1_2024"

# Sep–Nov 2021 (pre-inflation shock, textbook trends)
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2021-09-01 --end 2021-11-30 --name normal_trending_q4_2021"
```

---

## 9. Stress Tests (Compound Events)

### 9a. 2022 Full Chaos Year
Ukraine invasion + BoE hike cycle + Liz Truss + EUR parity — all in one year. The hardest possible environment for any rules-based system.

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2022-01-01 --end 2022-06-30 --name chaos_h1_2022"

docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2022-07-01 --end 2022-12-31 --name chaos_h2_2022"
```

### 9b. COVID + Trump Trade War Overlap (Jan–Jun 2020)
Tariff uncertainty ending, COVID beginning — two macro shocks colliding.

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest --start 2020-01-01 --end 2020-06-30 --name covid_plus_trade_war_h1_2020"
```

---

## 10. Recommended Test Sequence

Run these in order to build a complete performance profile:

| Priority | Command Name | What You're Testing |
|----------|-------------|---------------------|
| 1 | `baseline_with_story_fixes` | Confirm new code matches/beats original baseline |
| 2 | `summer_chop_2024` | False signal rate in low ATR |
| 3 | `trump_liberation_day_2025` | Extreme ATR spike handling |
| 4 | `boj_rate_hike_carry_unwind_2024` | Sustained extreme + zone invalidation |
| 5 | `iran_soleimani_jan_2020` | Spike-and-reverse (60% reversal in 5 days) |
| 6 | `russia_ukraine_invasion_2022` | Fundamental shift — OBs become worthless |
| 7 | `liz_truss_gbp_crash_2022` | Absolute drawdown limits |
| 8 | `normal_trending_q1_2024` | Best-case win rate ceiling |
| 9 | `svb_credit_suisse_march_2023` | Short-spike volatility |
| 10 | `year_end_thin_2024_2025` | Low-vol stop-hunt environment |

---

## Quick Reference — ATR Character Guide

| ATR Character | Description | Typical H1 ATR (EURUSD) | Bot Expectation |
|---------------|-------------|-------------------------|-----------------|
| Extreme | War/crash/shock | 80–200+ pips | High false-positive risk, story gates critical |
| High | Active trend, post-NFP | 40–80 pips | Bot's best performance window |
| Normal | Average week | 20–40 pips | Moderate performance, filter quality matters |
| Low | Summer chop, year-end | 8–20 pips | False signals, tight ranges trap entries |

---

## Notes

- All dates are inclusive. MT5 data availability may vary for pairs before 2015.
- If a period has no data, shorten the range by 2 weeks from each end and retry.
- Report files are saved to `src/backtest_results/` with the `--name` label as prefix.
- The MT5 clock in your Docker container runs ~1 year ahead (observed: results show 2026 timestamps for 2025 runs). This does not affect relative performance — it is a display issue only.
- For stress-test periods (COVID, Brexit), consider setting `BLOCK_COUNTER_D1=True` in config and re-running to compare — counter-trend entries should be suppressed in one-directional crashes.
