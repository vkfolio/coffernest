# Coffernest

A household money tracker you host yourself. Salary, home/personal/car loans, credit cards and
card EMIs, school fees and every bill that repeats, in one place. It shows what is genuinely free
to spend until the next salary, what's due, how heavy the debt load is, and how to clear it faster.

Coffernest is a ledger you fill in. It never connects to a bank, moves money, lends, or gives
regulated financial advice. Your data stays on the server you run.

- `api/`: FastAPI + Postgres. A client-agnostic `/api/v1` (OpenAPI at `/api/v1/docs`).
- iOS: the native Coffernest app connects to any Coffernest server, or runs entirely on the
  iPhone with no server.

**Run it:** see [SELF_HOSTING.md](SELF_HOSTING.md) (one `docker compose up`).

## What it does

| Area | Details |
|---|---|
| Ledger | Debits, credits and transfers; categories built for Indian households; tags; undo; offline quick add in 3 taps |
| Repeating | Monthly, quarterly, termly (school fees) and yearly rules. They appear as expected entries, a real payment within ±3 days and ±5% is matched to them, and auto-debits clear themselves |
| Loans | Exact amortisation schedule (to the paisa), floating-rate changes, prepayments (reduce tenure or reduce EMI), the EMI auto-debit kept in sync, and a prepayment simulator |
| Cards | Statements, minimum due, bill payments, utilisation against the 30% mark, EMI conversions and loans on the card (with 18% GST) |
| Insights | Safe to spend until payday, dues calendar, EMIs as a share of pay (FOIR), payoff plan (avalanche vs snowball), emergency-fund months, category trends, anomalies, 80C/24(b) |
| Advisor | Claude (Agent SDK) or any OpenAI-compatible model explains what to do, using read-only tools over the numbers. Every ₹ figure it writes is checked against those numbers |
| Household | The first account is the admin, who adds family by email; TOTP two-step sign-in; lockouts; session list |
| Files | Bills and statements stored in MinIO via presigned uploads |

## Local development

```bash
docker compose -f docker-compose.dev.yml up -d        # Postgres :5433, MinIO :9000
cd api && uv sync --extra ai && uv run coffernest migrate
uv run coffernest seed-demo                             # demo@example.com / demo-password-123, a year of data
uv run uvicorn coffernest.main:app --reload             # :8000
```

Checks (in `api/`): `uv run ruff check . && uv run mypy coffernest && uv run pytest`. The API
schema is at `/api/v1/openapi.json` on a running server.

To fill a running server instead (registration must be open while you do):
`uv run coffernest seed-demo --base-url https://your-server --email … --password …`. A server can
also keep its own demo account: see `SEED_DEMO` in [SELF_HOSTING.md](SELF_HOSTING.md).

## Sync

The iPhone app can keep its own copy of the books and sync it with a server
(`POST /api/v1/sync`). A device sends the rows it changed since its last sync, with the time
each was edited, and gets back what changed on the server since its cursor; the newest edit of
a row wins. When the device sends something the server already has under another id (a default
category, a person with the same name, the same bill occurrence) the answer's `id_map` tells it
to rename its copy. Changes are tracked by database triggers (migration 0008), so every other
route keeps working unchanged. Details are in `api/coffernest/services/sync.py`.

## License

[AGPL-3.0](LICENSE). You may run, change and share Coffernest. If you offer a changed version to
others over a network, you must publish your changes under the same license.
