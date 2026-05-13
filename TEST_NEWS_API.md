# Testing the News API

Use `test_news_api.py` to verify the `forex-api` container is reachable and returning events — replicating exactly how the bot calls it.

## Prerequisites

- Both containers must be running (`docker ps` should show `mt5` and `forex-api`)
- `.env` must have `NEWS_API_URL` and `NEWS_API_KEY` set

---

## Run the test

```powershell
docker exec mt5 python3 /bot/test_news_api.py
```

Checks both ForexFactory and MyFxBook endpoints for **USD** on today's date.

---

## Check a different symbol or date

```powershell
# GBP events
docker exec -e SYMBOL=GBP mt5 python3 /bot/test_news_api.py

# Specific date
docker exec -e DATE=2026-05-14 mt5 python3 /bot/test_news_api.py

# Both
docker exec -e SYMBOL=EUR -e DATE=2026-05-14 mt5 python3 /bot/test_news_api.py
```

---

## Expected output (success)

```
News API Smoke Test
  NEWS_API_URL : http://forex-api:8000
  NEWS_API_KEY : set (abcd…)

────────────────────────────────────────────────────────────
  FOREXFACTORY  |  symbol=USD  |  date=2026-05-13
  GET http://forex-api:8000/forexfactory/events?date=2026-05-13&currency=USD
────────────────────────────────────────────────────────────
  Status : 200
  Events : 3 returned
  Sample :
    {
        "title": "CPI m/m",
        "currency": "USD",
        "impact": "High",
        ...
    }
```

---

## Common errors

| Error | Cause | Fix |
|-------|-------|-----|
| `Missing API key` | `NEWS_API_KEY` not set in `.env` | Add key to `.env` |
| `Connection refused` / `URLError` | `NEWS_API_URL` wrong or `forex-api` not running | Check with `docker ps` and verify `NEWS_API_URL` in `.env` |
| `HTTP 401` | API key invalid | Check `NEWS_API_KEY` value in `.env` |
| `0 events returned` | No events for that date/currency | Try a weekday date or a different symbol |

---

## Quick connectivity check

If the test fails with a connection error, verify `mt5` can reach `forex-api`:

```powershell
docker exec mt5 curl -s http://forex-api:8000/health
```
