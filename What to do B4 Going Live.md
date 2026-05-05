## What I Would Check Before Going Live

### Critical Checks (Do These First)

1. **Spread & Slippage reality-check** — The backtester uses MT5 data but broker spreads in live differ. Pull your broker's average spread for each pair at session open and during news and compare to what the backtest assumed. If spreads are 2–3× wider than modeled, your avg win shrinks materially.

2. **Execution quality on limit orders** — The bot uses limit entries (OB taps). In live, partial fills, requotes, or skipped fills are real. Check whether the backtester modeled partial fills or assumed 100% fill. Look for `NOT_FILLED` outcomes — you saw 1 in the baseline (trade 177). Understand what percentage of limit orders won't fill in live.

3. **KES account sizing** — The backtest runs on 30,000 KES at 2% risk per trade. In live, 2% of 30k = 600 KES per trade. Confirm your lot sizes are achievable at your broker's minimum lot and that your broker accepts KES-denominated accounts (many require USD/EUR).

4. **Monitor connectivity** — The monitor runs bar-by-bar in backtest. In live, it depends on your VPS uptime, MT5 connection stability, and internet. If the monitor misses 1 stall signal in a live XAUUSD trade, you could hold a winner too long and watch it reverse. Test your VPS reliability.

5. **BoJ/JPY behavior in live** — USDJPY was the weakest pair (50% win rate in baseline) and had the most monitor closes (20 of 52 trades). In live, USDJPY moves fast and stop-hunts are common. I'd run it at reduced risk (1% instead of 2%) until you confirm the live behavior matches backtest.

6. **USDCHF confidence** — Only 10 trades in 6 months, 40% win rate in the baseline. Statistically too small a sample to trust. Consider running USDCHF at 0.5% risk or disabling it in live until you accumulate more data.

### Secondary Checks

7. **Month 5 (Mar 2026) anomaly** — That month generated +89,471 KES, which is 81% growth in a single month and accounts for 39% of the entire 6-month gain. That's extreme concentration. If live month 5 is a slow month, your equity curve will look terrible and you'll be tempted to change the bot. Don't. Check the underlying GBPUSD trades in March — was this driven by a specific macro trend that may not repeat?

8. **Account leverage and margin** — At peak equity (259k KES), the bot may open multiple concurrent positions. Confirm your broker's margin requirements don't force position liquidation when holding 3–4 symbols simultaneously.

9. **Timezone and session configuration** — The MT5 Docker clock runs ~1 year ahead (noted in BACKTEST_COMMANDS.md). Confirm the session open/close times and news filter times are aligned to your broker's server time in live, not the Docker offset.

---

## How Long Should Demo Before Live?

**My answer: 3 full months minimum, targeting at least 60 trades.**

Here's why that specific number:

- 60 trades gives you statistical significance on win rate (margin of error ~12% at 95% confidence). At ~31 trades/month in the baseline, you'll hit 60 trades in about 2 months — but you want a full calendar quarter to catch month-end liquidity shifts, at least one central bank cycle, and post-NFP behavior
- **Do not go live based on 1–2 weeks** even if it looks great — you could be in a high-momentum window that flatters the bot
- **Do not abandon demo after 2–3 weeks** if it looks bad — you could be in the "normal trending low-ATR" regime where the bot underperforms by design (win rate 43–47%)
- Month 1 in the baseline was +47% — if your demo month 1 is only +8%, that's normal variance, not a broken bot

### Demo Checklist Before Switching to Live

| Gate | What to Verify |
|---|---|
| 60+ trades completed | Statistical sample is meaningful |
| Win rate ≥ 45% sustained | Bot has edge in this market regime |
| No single drawdown > 20% equity | Risk sizing is correct |
| Monitor signals firing correctly | VPS/connectivity is stable |
| No unexpected NOT_FILLED patterns | Limit order fills are working |
| At least 1 NFP week lived through | News filter working correctly in live |
| At least 1 month-end thin week | Year-end / low liquidity handled |

---

## Bottom Line

This bot has a genuine, tested edge across 10 years of market history including COVID, Brexit, wars, and bank collapses. **It never lost money in any of the 34 tests.** The monitor system is exceptional. The biggest real-world risks are: execution quality, spread differences, USDJPY/USDCHF unreliability, and the bot's known weakness in slow/choppy no-catalyst markets (where you'll be psychologically tempted to interfere with it).

**Go 3 months demo, monitor the 6 gates above, then go live at 1% risk (not 2%) for the first live month before scaling back to 2%.**