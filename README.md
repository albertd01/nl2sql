# nl2sql

General natural-language-to-SQL demo agent. Works against any SQLAlchemy database
(tested: SQLite, Postgres), compares Gemma 4 31B, Qwen 3.8 27B and DeepSeek V4 Flash
via OpenRouter at their highest served precision.

## Setup

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env                               # add OPENROUTER_API_KEY
uv run pytest -q
```

If the project sits in an iCloud-synced folder (e.g. macOS Desktop), iCloud marks files inside
`.venv` as hidden and Python 3.13 skips the editable-install `.pth` file (`import nl2sql` fails).
Keep the virtualenv elsewhere: `export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/nl2sql`.

## Data (not included)

The agent works with any database URL. The examples, evaluation and demos use:

| Data | Where the code looks | Override |
|---|---|---|
| MIMIC-IV demo in the EHRSQL SQLite format (dates shifted into the 2100s), plus the EHRSQL answerable/unanswerable question files — as used by the M3 paper reproduction | `../m3_repro/db/mimic_iv.sqlite`, `../m3_repro/data/` | `NL2SQL_MIMIC_DB`, `NL2SQL_MIMIC_DATA` |
| BIRD mini-dev SQLite databases + canonical questions from Hugging Face (`birdsql/bird_mini_dev`) | `~/.local/share/nl2sql/bird` | `NL2SQL_BIRD_ROOT` |
| Chinook sample database on a local Postgres | `scripts/chinook_postgres.sh start` | any SQLAlchemy URL |

## Usage

```bash
# Inspect what the agent sees
uv run nl2sql schema --db sqlite:///../m3_repro/db/mimic_iv.sqlite
uv run nl2sql schema --db sqlite:///../m3_repro/db/mimic_iv.sqlite --table admissions

# Ask one model, or all three in parallel
uv run nl2sql ask --db sqlite:///../m3_repro/db/mimic_iv.sqlite \
  --reference-time "2100-12-31 23:59:00" --model all \
  "How many female patients were admitted as urgent this year?"

# Postgres sample database
scripts/chinook_postgres.sh start
uv run nl2sql ask --db postgresql+psycopg://nl2sql_ro@127.0.0.1:55432/chinook --model qwen \
  "Which 5 countries generated the most revenue?"
```

`--json runs/x.jsonl` appends full results (every step, SQL, tokens, cost, provider).

## Live demo (Open WebUI)

Open WebUI as the chat UI, Gemma via OpenRouter, and the agent's database tools served over MCP.
Tool calls (inputs and results) show up inside the chat.

```bash
uv tool install --python 3.11 open-webui        # once; Open WebUI needs Python 3.11/3.12
openwebui/start.sh                               # MCP server :8765 + Open WebUI :8080 (no login)
UV_PROJECT_ENVIRONMENT=~/.venvs/nl2sql uv run python openwebui/configure.py   # once, idempotent
open http://127.0.0.1:8080
openwebui/stop.sh
```

- `start.sh` reads `OPENROUTER_API_KEY` from the environment, `nl2sql/.env` or `../m3_repro/.env`;
  Open WebUI data lives in `~/.local/share/open-webui`, logs in `~/.local/share/nl2sql/logs`.
- `configure.py` limits the connection to Gemma, creates the **SQL Agent · MIMIC-IV** preset
  (chat system prompt from `nl2sql prompt --chat`, native tool calling, MCP tools on by default,
  bf16 provider pin via `custom_params`, the 4 demo questions as starters) and makes it the default.
- Open WebUI runs the tool loop here, not `nl2sql.agent`: same tools and prompt rules, but no
  self-check, forced final answer or structured final_answer tool, so results can differ from the eval.
- Open WebUI's own built-in tools are switched off for the preset: they add ~27 tool definitions
  and Venice rejects Gemma requests with more than 20.

## Demo page

A static page showing 4 recorded runs on the MIMIC-IV demo database (question → tool calls →
answer, SQL, result). Recording and page are separate, so the page never depends on a live call:

```bash
uv run --env-file ../m3_repro/.env python demo/record.py   # runs the prompts -> demo/traces.json
uv run python demo/build_page.py                           # -> demo/sql_agent_demo.html
```

Prompts live in `demo/record.py`; the per-question notes shown on the page live in
`demo/template.html` (`NOTES`) — re-check them after re-recording, since they describe what the
agent did in that recording.

## Evaluation

Suites (fixed, stratified, seeded splits — tune on `dev`, run `test` once at the end):

| Suite | Source | dev | test | Notes |
|---|---|---|---|---|
| `mimic` | `../m3_repro/data` (EHRSQL on MIMIC-IV demo) | 60 answerable (10 tie) + 60 unanswerable | 40 + 40 | reference time 2100-12-31 23:59 |
| `bird` | BIRD mini-dev (SQLite), 11 databases | 153 | 153 | stratified by database × difficulty; 194 held in `reserve`; evidence hints passed by default |

BIRD databases (~1.4 GB) live outside iCloud in `~/.local/share/nl2sql/bird`
(`NL2SQL_BIRD_ROOT` to override): `minidev.zip` from the BIRD mini-dev README, plus the
canonical question file from Hugging Face saved as `mini_dev_sqlite_hf.json`.

```bash
uv run nl2sql eval prepare                       # -> evals/suites/*.jsonl
uv run nl2sql eval check-gold                    # every gold query runs? matches stored answers?
uv run nl2sql eval run runs/eval/NAME --split dev --concurrency 16   # resumable
uv run nl2sql eval score runs/eval/NAME          # execution match, then LLM judge on mismatches
uv run nl2sql eval report runs/eval/NAME         # -> runs/eval/NAME/report.md
```

Scoring:
1. **EX** — gold SQL and the agent's final SQL are executed and compared as result sets
   (order/duplicates ignored, numbers to 4 significant figures, strings case-folded, extra
   columns allowed if a projection reproduces the gold result).
2. **Correct** — EX, or Claude Sonnet 5 (via OpenRouter) judges the answer text equivalent
   to the gold answer. Catches right answers with differently shaped SQL (yes/no questions,
   "peak month = 04" answered as "April 2013"). Judge also labels an error type.
3. Unanswerable questions are correct only if the agent returns status `unanswerable`.

### Agent flags (Milestone 3)

`--config flag1,flag2` on `ask` and `eval run`; all off = baseline. Measured with Gemma on the
dev split against the baseline (fixed/broken questions out of 273; an unchanged rerun flipped 2).

| Flag | What it does | Result | Keep? |
|---|---|---|---|
| `abstain_rule_v2` | Rules for unanswerable questions: missing data (no stand-in columns), nonexistent entities, advice/action requests | **8 fixed / 0 broken**; refusals 76.7 → 83.3% | **yes** |
| `force_final` | Warn at 4 turns left; last turn may only call `final_answer` | 4 / 1 — Gemma rarely hits the limit | yes (safeguard) |
| `self_check` | Final SQL re-run before accepting the answer: fails? empty? | neutral in combination | yes (safeguard) |
| `abstain_rule` | v1 of the answerability rules | 6 / 3 — "no such patient" answered as "no records" | superseded |
| `value_check` | Prompt rule + `run_query` warns on filter literals absent from their column | 6 / 4 — warning fired once in 273 runs | no |
| `tie_rule` | "Top N includes ties" via DENSE_RANK | 5 / 8 — BIRD gold cuts ties | no |
| `tie_check` | Self-check flags `LIMIT` cutting through a tie | 3 / 7 (run together with `self_check`) | no (benchmarks disagree on ties; scored strictly) |

Recommended: `--config abstain_rule_v2,force_final,self_check` — dev: BIRD 77.8%, MIMIC answerable
83.3%, MIMIC refusals 83.3% (baseline 76.5 / 81.7 / 76.7).

Compare two runs question by question: `uv run nl2sql eval compare runs/eval/A runs/eval/B --model gemma`.
Error labels (taxonomy v2) are assigned after scoring; `nl2sql eval relabel RUN` redoes them.

Known benchmark noise: BIRD gold SQL sometimes contradicts its own evidence hint (e.g.
bird-1482 divides by the 2012 value while the hint says 2013), and one EHRSQL stored answer
contradicts its gold SQL (overridden in `datasets.py`).

## Layout

| Path | What |
|---|---|
| `src/nl2sql/db/connect.py` | Read-only connections (SQLite `mode=ro`; Postgres/MySQL read-only sessions + statement timeouts) |
| `src/nl2sql/db/introspect.py` | Schema cards: columns, keys, row counts, sample rows, low-cardinality values; cached in `.cache/` |
| `src/nl2sql/tools/sql_check.py` | sqlglot validation: single read-only statement, blocked functions, known tables/columns |
| `src/nl2sql/tools/toolbox.py` | `list_tables`, `describe_table`, `search_values`, `check_sql`, `run_query`, `final_answer` |
| `src/nl2sql/agent/models.py` | Model registry + OpenRouter precision pins |
| `src/nl2sql/agent/loop.py` | Tool-calling loop with step events (for the UI) and token/cost/provider accounting |
| `src/nl2sql/eval/datasets.py` | Suite builders, splits, database resolution |
| `src/nl2sql/eval/compare.py` | Result-set comparison (EX) |
| `src/nl2sql/eval/scoring.py` | Gold execution cache, EX, LLM judge + error types |
| `src/nl2sql/eval/runner.py`, `report.py` | Concurrent resumable runs; markdown report with Wilson CIs |

## License

MIT — see [LICENSE](LICENSE).

## Safety model

Two independent layers: the validator rejects anything but a single SELECT/WITH, and the
connection itself is read-only. For Postgres/MySQL outside the demo, also connect as a
role with only SELECT grants — session settings are the weakest of the three layers.

## Model precision pins

OpenRouter endpoints checked 2026-09-15. Requests never fall back to lower-precision or
unlabeled endpoints; if all allowed providers are down, the call fails visibly.

| Key | Model | Pin | Providers at that precision |
|---|---|---|---|
| `gemma` | google/gemma-4-31b-it | bf16 | Venice, Novita |
| `qwen` | qwen/qwen3.8-27b | bf16 | DeepInfra |
| `deepseek` | deepseek/deepseek-v4-flash | fp8 (no bf16 endpoint exists) | DeepInfra, Novita, Parasail, … |
