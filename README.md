# nl2sql

A natural-language-to-SQL agent. You ask a question in plain English; the agent inspects the
database schema, looks up how values are actually stored, writes read-only SQL, runs it and
answers — or says so when the data isn't there. Works with any SQLAlchemy database (tested:
SQLite, Postgres). The live demo runs Gemma 4 31B via OpenRouter on the MIMIC-IV demo database
inside Open WebUI, with every tool call visible in the chat.

**No setup at all:** download [`demo/sql_agent_demo.html`](demo/sql_agent_demo.html) and open it
in a browser — it shows recorded runs of four questions, step by step.

## Try the demo

These steps are written to be followed top to bottom by a person or by a coding agent. Every
step is safe to re-run. Time: about 10 minutes, most of it downloads. Cost: well under
1 cent per question.

### 1. Check the prerequisites

| Requirement | Check | If missing |
|---|---|---|
| macOS or Linux (Windows: use WSL2) | `uname -s` | — |
| git, curl | `git --version && curl --version` | install via your package manager |
| lsof | `command -v lsof` | Debian/Ubuntu: `sudo apt install lsof` (macOS has it) |
| uv | `uv --version` | `curl -LsSf https://astral.sh/uv/install.sh \| sh`, then open a new shell |
| ~3 GB free disk, ~2 GB free RAM | — | — |
| OpenRouter API key with credits | the user has one | create at https://openrouter.ai/settings/keys |
| Ports 8080 and 8765 free | `lsof -nP -iTCP:8080 -iTCP:8765 -sTCP:LISTEN` prints nothing | use other ports, see [Troubleshooting](#troubleshooting) |

uv installs the Python versions this needs (3.13 for this project, 3.11 for Open WebUI) by itself.

### 2. Get the code

```bash
git clone https://github.com/albertd01/nl2sql.git
cd nl2sql
```

All following commands run from this `nl2sql` folder.

### 3. Install Open WebUI (once, ~2 GB)

```bash
uv tool install --python 3.11 open-webui
```

Check: `uv tool list` lists `open-webui`.

### 4. Download the database (once, 37 MB)

```bash
scripts/get_mimic_db.sh
```

Expected last line: `Saved and verified: .../data/mimic_iv.sqlite` (or `Already present and verified`).

### 5. Add the OpenRouter API key

Create a file `.env` in the project folder containing one line:

```
OPENROUTER_API_KEY=sk-or-v1-...
```

`.env` is git-ignored. The scripts read the key from there and never print it.

### 6. Start the demo

```bash
openwebui/start.sh
```

It prepares the Python environment, starts the database tool server (MCP, port 8765) and
Open WebUI (port 8080), waits until Open WebUI is ready, and configures it. The first start can
take several minutes. Expected last line:

```
READY: http://127.0.0.1:8080
```

### 7. Verify it works

```bash
uv run --no-project --with httpx python openwebui/check.py
```

This asks one question through Open WebUI exactly like the chat does and prints the tool calls
and the answer. Expected: one or more `tool call: mimic_...` lines, an answer saying that 24
female patients were admitted through the emergency room, and finally
`OK: the demo works end to end` (exit code 0).

### 8. Use it

Open http://127.0.0.1:8080 in a browser (no login). The model **SQL Agent · MIMIC-IV** is
preselected; click one of the four example questions or type your own, e.g.:

- How many female patients were admitted through the ER?
- Which 5 lab tests were performed most often, and how many times each?
- What is the average ICU stay in days for patients diagnosed with atrial fibrillation?
- What is the phone number of the doctor who treated patient 10004235? *(it should decline)*

Each tool call appears in the answer as an expandable block with its input and result.

### 9. Stop

```bash
openwebui/stop.sh
```

Start again any time with `openwebui/start.sh` (steps 3–5 are not needed again).

### Notes for coding agents

- Follow the steps in order. The only thing to ask the user for is the OpenRouter API key
  (step 5). Write it only to `.env`; never print, log or commit it.
- Commands don't depend on shell state (no `export` needed), so running each step in a fresh
  shell is fine — just run them from the `nl2sql` folder.
- `openwebui/start.sh` blocks until Open WebUI is ready (up to 6 minutes on a first start). Use a
  long command timeout, or run it in the background and poll `curl -fs http://127.0.0.1:8080/health`.
- Success criterion: `openwebui/check.py` exits 0 and prints `OK: the demo works end to end`. Then
  tell the user to open http://127.0.0.1:8080.
- Never stop or kill other programs that use ports 8080 or 8765; set different ports instead
  (see Troubleshooting). `openwebui/stop.sh` only stops the demo's own processes.
- On failure, the scripts print an `ERROR:` line with the fix; logs are in `~/.local/share/nl2sql/logs/`.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ERROR: Port 8080 is used by another program` (or 8765) | another app uses the port | pick free ports and prefix every demo command with them: `NL2SQL_OW_PORT=8090 NL2SQL_MCP_PORT=8795 openwebui/start.sh`, `NL2SQL_OW_URL=http://127.0.0.1:8090 uv run --no-project --with httpx python openwebui/check.py`, `NL2SQL_OW_PORT=8090 NL2SQL_MCP_PORT=8795 openwebui/stop.sh`; then open http://127.0.0.1:8090 |
| `ERROR: Open WebUI not installed` | step 3 missing, or uv's tool directory isn't found | `uv tool install --python 3.11 open-webui`; check `uv tool dir --bin` |
| `ERROR: MIMIC-IV database not found` | step 4 missing | `scripts/get_mimic_db.sh` |
| `ERROR: OPENROUTER_API_KEY not set` | step 5 missing | create `.env` as in step 5 |
| `Open WebUI exited during startup` | see the log lines printed below the error | most often an install built for a different Python: `uv tool install --reinstall --python 3.11 open-webui` |
| `check.py`: `Provider returned error` | an OpenRouter provider rejected the request or is down | retry in a minute; details in `~/.local/share/nl2sql/logs/open-webui-8080.log` |
| `check.py`: `401` / `User not found` | invalid API key | fix the key in `.env`, then `openwebui/stop.sh && openwebui/start.sh` |
| `check.py`: `402` / insufficient credits | the OpenRouter account has no credits | add credits at https://openrouter.ai/settings/credits |
| `No endpoints found` | no full-precision (bf16) Gemma provider is available right now | retry later; to allow 8-bit providers temporarily, set `quantizations=("bf16", "fp8")` for `gemma` in `src/nl2sql/agent/models.py` and restart |
| Answer without any tool calls / "cannot access the database" | the MCP server isn't running | `openwebui/stop.sh && openwebui/start.sh`; see `~/.local/share/nl2sql/logs/mcp-8765.log` |
| `ModuleNotFoundError: No module named 'nl2sql'` | project folder synced by iCloud (macOS Desktop/Documents) with a virtualenv inside it | the demo scripts already use `~/.venvs/nl2sql`; for your own `uv run` commands `export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/nl2sql"` |
| Start everything from scratch | — | `openwebui/stop.sh && rm -rf ~/.local/share/open-webui` (deletes Open WebUI chats and settings), then `openwebui/start.sh` |

### How the live demo works

- **Open WebUI** is the chat UI and runs the tool-calling loop. It talks to **Gemma 4 31B** through
  OpenRouter, pinned to full-precision (bf16) providers.
- **`nl2sql mcp`** serves the database tools over MCP: `list_tables`, `describe_table`,
  `search_values`, `check_sql`, `run_query`. The database is opened read-only and only single
  SELECT queries are accepted.
- **`openwebui/configure.py`** (run by `start.sh`) creates the **SQL Agent · MIMIC-IV** preset: the
  system prompt with the schema and answerability rules (`nl2sql prompt --chat`), native tool
  calling, the MCP tools enabled by default, the four example questions, and the provider pin.
  Open WebUI's own built-in tools are switched off for the preset: they would add ~27 tool
  definitions, and one of the Gemma providers rejects requests with more than 20.
- Open WebUI keeps its data in `~/.local/share/open-webui`; logs and process IDs are in
  `~/.local/share/nl2sql`; the project's Python environment is `~/.venvs/nl2sql`
  (or `$UV_PROJECT_ENVIRONMENT`). Everything listens on 127.0.0.1 only.
- In Open WebUI the tool loop is Open WebUI's, not `nl2sql.agent`: same tools and prompt rules, but
  no self-check or forced final answer, so answers can differ slightly from the evaluated agent.

## Development setup

Requires [uv](https://docs.astral.sh/uv/) (Python 3.13 is installed automatically).

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
| MIMIC-IV demo database in EHRSQL 2024 format (dates shifted into the 2100s): `scripts/get_mimic_db.sh` downloads it from [EHRSQL 2024](https://github.com/glee4810/ehrsql-2024) (pinned commit, checksum-verified). Built from the [MIMIC-IV Clinical Database Demo 2.2](https://physionet.org/content/mimic-iv-demo/2.2/) (ODbL); EHRSQL's `preprocess/preprocess.sh` reproduces the same table contents | `data/mimic_iv.sqlite`, else `../m3_repro/db/mimic_iv.sqlite` | `NL2SQL_MIMIC_DB` |
| EHRSQL answerable/unanswerable question files from the M3 paper reproduction (evaluation only) | `../m3_repro/data/` | `NL2SQL_MIMIC_DATA` |
| BIRD mini-dev SQLite databases + canonical questions from Hugging Face (`birdsql/bird_mini_dev`) | `~/.local/share/nl2sql/bird` | `NL2SQL_BIRD_ROOT` |
| Chinook sample database on a local Postgres | `scripts/chinook_postgres.sh start` | any SQLAlchemy URL |

## Usage

```bash
# Inspect what the agent sees
uv run nl2sql schema --db sqlite:///data/mimic_iv.sqlite
uv run nl2sql schema --db sqlite:///data/mimic_iv.sqlite --table admissions

# Ask one model, or all three in parallel
uv run nl2sql ask --db sqlite:///data/mimic_iv.sqlite \
  --reference-time "2100-12-31 23:59:00" --model all \
  "How many female patients were admitted as urgent this year?"

# Postgres sample database
scripts/chinook_postgres.sh start
uv run nl2sql ask --db postgresql+psycopg://nl2sql_ro@127.0.0.1:55432/chinook --model qwen \
  "Which 5 countries generated the most revenue?"
```

`--json runs/x.jsonl` appends full results (every step, SQL, tokens, cost, provider).

## Demo page

A static page showing 4 recorded runs on the MIMIC-IV demo database (question → tool calls →
answer, SQL, result). Recording and page are separate, so the page never depends on a live call:

```bash
uv run --env-file .env python demo/record.py   # runs the prompts -> demo/traces.json
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
| `openwebui/start.sh`, `stop.sh`, `check.py`, `configure.py` | Live demo: start/stop, headless end-to-end check, Open WebUI configuration |
| `scripts/get_mimic_db.sh` | Download + verify the MIMIC-IV demo database |
| `src/nl2sql/mcp_server.py` | Database tools over MCP (Streamable HTTP) for chat UIs |
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
| `gemma` | google/gemma-4-31b-it | bf16 | Venice, Novita, Crusoe |
| `qwen` | qwen/qwen3.8-27b | bf16 | DeepInfra |
| `deepseek` | deepseek/deepseek-v4-flash | fp8 (no bf16 endpoint exists) | DeepInfra, Novita, Parasail, … |
