# ForexAI

[![Tests](https://github.com/Sina-Shafeian/forexai/actions/workflows/ci.yml/badge.svg)](https://github.com/Sina-Shafeian/forexai/actions/workflows/ci.yml)

A local forex analysis and Telegram signal bot with explainable, rule-based signals
and optional, user-confirmed execution on MetaTrader 5 **demo accounts**.

The signal engine combines market structure, multiple timeframes, technical
indicators, currency strength, and economic-calendar filters. It does not use a
trained machine-learning model or an LLM in its runtime signal pipeline.

[راهنمای فارسی](README.fa.md)

## Features

- Seven configured instruments: EURUSD, GBPUSD, USDJPY, USDCHF, USDCAD, AUDUSD, and XAUUSD.
- Closed-candle analysis across D1, H4, H1, and M15.
- Market structure: swing points, HH/HL/LH/LL, BOS/CHOCH, and liquidity sweeps.
- ATR, EMA, RSI, and ADX confirmations, with configurable scoring and filters.
- Currency-relative strength, correlation checks, and a configurable daily alert limit.
- Economic-calendar filtering through Myfxbook RSS, with optional FMP support.
- Broker-clock normalization to UTC and configurable Telegram display timezone.
- Parquet market snapshots and SQLite decision/execution state.
- Telegram signals with a read-only recheck button and an explicit demo-execution button.
- Position, session, entry-deviation, margin, and SL/TP checks before execution.
- Persistent execution reservations to prevent repeated execution of the same signal.

## Execution modes

| Mode | Shipped behavior |
| --- | --- |
| Console (`--console`) | Analysis output without Telegram dispatch or order execution |
| Telegram signal delivery | Sends qualifying signals; sending a signal does not place an order |
| Semi-automatic execution | Enabled in the sample configuration; requires an explicit click from the authorized Telegram user and a demo account |
| Full automatic execution | Disabled in the sample configuration |

The execution adapter independently checks that the connected account is a demo
account before accessing the order API. This repository is intended for development
and demo evaluation. Its confidence score is a rule-based score, not a calibrated
probability of winning; historical performance and profitability are not established.

## Requirements

- **Windows** for the MT5 runtime, with an installed MetaTrader 5 terminal and a logged-in demo account.
- Python **3.12**; `.python-version` selects **3.12.7**.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) for dependency management.
- A Telegram bot token and chat ID for Telegram delivery. Console mode does not need them.

The MetaTrader5 Python dependency is installed only on Windows. Unit tests can run
on Linux without MT5; the trading runtime cannot. GitHub Actions runs unit tests on
Linux and Windows without broker credentials or a connected terminal.

## Installation

In PowerShell:

```powershell
git clone https://github.com/Sina-Shafeian/forexai.git
cd forexai
uv python install 3.12.7
uv sync --locked --dev
Copy-Item .env.example .env
```

When updating an existing checkout, keep your local `.env`; do not overwrite it.

Configure the following files for your environment:

| File | Purpose |
| --- | --- |
| `config/app.yaml` | MT5 terminal path, timeframes, runtime options, execution modes, and risk settings |
| `config/instruments.yaml` | Broker-specific symbol names, including suffixes and prefixes |
| `config/scoring.yaml` | Signal score weights, thresholds, and diversification filters |
| `config/events.yaml` | Economic-calendar provider and fallback behavior |
| `.env` | Local Telegram credentials and optional FMP API key |

The default terminal path is `C:/Program Files/MetaTrader 5/terminal64.exe`; change
it if your installation is elsewhere. In MT5, show the configured symbols in
Market Watch and load enough history for the configured timeframes.

## MT5 session exporter

The project includes an MQL5 service that reads symbol trading-session schedules
and exports them to MT5 Common Files. Install the provided source and compiled service:

```powershell
.\scripts\install_session_exporter.ps1
```

In MT5, open **Navigator → Services**, refresh the list, and start
`ForexAISessionExporter`. It refreshes the exported schedules every five minutes.
Missing or stale session data suppresses signals with `SESSION_STATUS_UNAVAILABLE`.

The exporter source is in `mt5/ForexAISessionExporter.mq5`; its compiled artifact is
included because the installer expects it.

## First run

Start with diagnostics and console output:

```powershell
uv run forexai check
uv run forexai time-check
uv run forexai market-check
uv run forexai scan-once --console
```

For continuous console analysis:

```powershell
uv run forexai run --console
```

Stop the loop with `Ctrl+C`. These runtime commands still require Windows, MT5,
market data, and the session exporter; console mode does not simulate a market feed.

## Telegram setup

1. Create a bot through Telegram's `@BotFather` using `/newbot`.
2. Open a private conversation with your new bot and send `/start`.
3. Put its token and your chat ID in the local `.env` file.
4. For a group or channel, also set `TELEGRAM_ALLOWED_USER_ID` to the specific user
   permitted to execute signals. A positive private-chat ID is used as the user ID by default.

The empty credential template is `.env.example`. Actual credentials belong only in
your local `.env`, which Git ignores. The Persian guide includes a PowerShell
example for retrieving the chat ID through Telegram's `getUpdates` endpoint.

Send an independent test message:

```powershell
uv run forexai telegram-test
```

Then deliver signals continuously:

```powershell
uv run forexai run
```

With the shipped configuration, this starts both the scan loop and the Telegram
callback listener. The recheck button reads current entry conditions. The execution
button requires an authorized user, a fresh persisted signal, a demo account, and
successful execution checks. Full automatic execution remains disabled.

Read-only execution diagnostics are available separately:

```powershell
uv run forexai execution-check
uv run forexai semi-execution-check
```

These diagnostic commands do not call `order_check` or `order_send`.

## Economic calendar

The default provider is Myfxbook RSS. FMP is an optional alternative configured in
`config/events.yaml`; it requires a local `FMP_API_KEY` and appropriate API access.

```powershell
uv run forexai calendar --hours 48 --importance High --limit 20
```

The default `unavailable_behavior: warn_and_continue` logs a warning when calendar
data is unavailable and allows analysis to continue. Use `block` in the configuration
if calendar unavailability should block candidates.

## Development and tests

```bash
uv sync --locked --dev
uv run --locked pytest
```

The unit suite uses synthetic data and fake broker/Telegram clients. It covers
configuration, signal models, market/session filters, position sizing, callback
authorization, duplicate execution handling, demo-account restrictions, and secret
redaction. These tests do not establish strategy performance or replace a real
Windows/MT5 integration check.

## Architecture

```text
src/forexai/
  domain/        Signal, instrument, account, and execution models
  ports/         Interfaces for market data, economic calendars, and notifications
  adapters/      MT5, Telegram, calendar, Parquet, and SQLite integrations
  analysis/      Rule-based analysis, scoring, filters, and execution coordination
  application/   Scan orchestration and dispatch
  cli.py         Commands, console output, and runtime setup
config/          Sample runtime configuration
mt5/             Session-exporter source and compiled service
scripts/         Windows PowerShell launchers and exporter installer
tests/           Unit tests and synthetic fixtures
```

Runtime snapshots, decision databases, logs, credentials, virtual environments,
and local assistant tooling are excluded from this repository. Empty data directories
are retained with `.gitkeep` files and populated by the application as needed.
