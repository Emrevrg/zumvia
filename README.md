<p align="center">
  <img src="assets/brand-full/logo-512.png" alt="ZUMVIA" width="320">
</p>

<h1 align="center">ZUMVIA</h1>

<p align="center">
  <b>Your autonomous AI command center for managing money.</b><br>
  Zero-Hallucination · Neuro-Symbolic · Open Source
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-00e676?style=flat-square" alt="MIT">
  <img src="https://img.shields.io/badge/python-3.11%2B-00e676?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/tests-943%20passing-00e676?style=flat-square" alt="Tests">
  <img src="https://img.shields.io/badge/mode-paper%20(default)-1e9e5a?style=flat-square" alt="Paper">
  <img src="https://img.shields.io/badge/MCP-supported-00e676?style=flat-square" alt="MCP">
</p>

You do not need to know finance. Write one sentence — *“Manage my $1,000 in
crypto and handle the rest”* — and ZUMVIA reads the market, cross-checks every
view with a deterministic algorithmic engine, researches news, validates ideas
with walk-forward testing, deploys and monitors bots, and shows every step.

> **Important:** This software is not financial advice and does not guarantee
> profit. Trading can lose capital. ZUMVIA defaults to paper mode; measure at
> least 30 days of paper results before considering live trading.

## Why ZUMVIA?

ZUMVIA combines a conversational command agent with deterministic mathematics
and an unbreakable risk layer. LLMs interpret prepared numbers; Python computes
the numbers. If a model, provider, network, or API quota fails, the algorithmic
engine can continue without it.

## Interface

- **Command center:** chat, visible tool-call cards, raw input/output and timing.
- **Finance workspace:** live market board, search and filters, charts, news and
  a context-aware finance assistant.
- **Control center:** emergency brake, live authorization, keys, model registry,
  reports, MCP connections and immutable limits.
- **Command palette:** press `Ctrl+K` to search pages, tasks, bots and actions.
- Responsive, animated Next.js UI with a consistent SVG icon system.

## Decision modes

| Mode | Behavior | Best for |
| --- | --- | --- |
| Automatic | One model with one key; council with multiple keys | Default |
| Single model | One model decides, risk code still applies | Speed and cost |
| Council | Parallel opinions, weighted votes and median levels | Larger capital |
| Conservative | Council plus risk critic, veto and referee | Highest quality |

Split opinions do not open a trade. Waiting in uncertainty is a feature.

## Architecture

1. **Market data:** ccxt (100+ exchanges), yfinance and a deterministic demo feed.
2. **Math engine:** RSI, EMA, MACD, Bollinger Bands, ATR, ADX, Supertrend,
   Stochastic and VWAP — no LLM-generated numbers.
3. **Strategy engine:** 12 classical strategies, parallel scanner and
   walk-forward validation.
4. **Model council:** Gemini, DeepSeek, Claude, Qwen, OpenAI, Groq, Ollama/vLLM
   and OpenAI-compatible providers.
5. **Risk shield:** mandatory stop-loss, 1% per-trade cap, minimum 1:2 R/R,
   circuit breaker, portfolio heat, correlation and spread filters.
6. **Execution:** paper/live modes, scale-out, reconciliation and Telegram
   notifications. There is no money-transfer or withdrawal tool.

## Installation

### Docker (recommended)

Requires only **Docker**:

```bash
docker compose up -d
```

Open `http://localhost:8000`. The optional Next.js UI runs on port 3000:

```bash
docker compose --profile web up -d
```

The container binds to localhost by default. Put it behind HTTPS or a VPN before
exposing it to a network.

### Python development

Requires **Python 3.11+**. The backend includes a built-in UI; Node.js is only
needed when developing the optional Next.js frontend.

```bash
git clone https://github.com/Emrevrg/zumvia.git zumvia
cd zumvia/backend
python -m venv .venv
```

Activate the environment, then:

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For the optional frontend:

```bash
cd ../frontend
npm install
npm run dev
```

## First use

1. Create an account (data stays in your configured database).
2. Add an AI provider key under **Profile → Settings → Add key**, or choose
   **Algorithm only** to run without an AI key.
3. In the command center, describe what you want in one sentence.

## Control agents and MCP

ZUMVIA exposes **69 tools** through Model Context Protocol. It supports Claude
Code, Claude Desktop, Codex, Cursor and other MCP clients. Create and revoke
short-lived keys under **Control Center → Connections**; only SHA-256 hashes are
stored.

HTTP transport:

```text
http://127.0.0.1:8000/mcp
Authorization: Bearer <TOKEN>
```

Claude Code example:

```bash
claude mcp add --transport http zumvia http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer <TOKEN>"
```

Local stdio transport:

```bash
claude mcp add zumvia -- python /full/path/to/zumvia/backend/mcp_server.py
```

All MCP calls pass through the same risk shield as the web UI.

## Risk shield

The following rules are enforced in code and cannot be bypassed by a user,
agent, model or MCP client:

- Maximum 1% account risk per trade and mandatory stop-loss.
- Minimum 1:2 risk/reward and confidence threshold.
- Daily circuit breaker and configurable drawdown lock.
- Portfolio heat, correlation and spread limits.
- Unmeasured open risk is rejected; it is never silently treated as zero.
- Live authorization is limited, time-bound, revocable and paper mode is the
  default. Withdrawal and money-transfer operations do not exist.

## Development and tests

```bash
cd backend
python -m pytest tests -q
```

The suite contains **943 tests** covering risk rules, indicators, schema
validation, agent loops, councils, portfolio protection, recovery, walk-forward
validation, live authorization, MCP transport, CLI adapters and API endpoints.

Useful checks:

```bash
python tools/verify_all.py
ruff check app tests
cd ../frontend && npm run build
```

## Project structure

```text
backend/       FastAPI service, agent, math/strategy engines, risk and tests
frontend/      Optional Next.js command and finance workspace
assets/        ZUMVIA brand mark and full logo
docs/          MCP and operational documentation
```

## License

MIT. See [LICENSE](LICENSE). Contributions are welcome; changes that weaken the
risk shield are not accepted.
