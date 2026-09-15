"""Evaluation suites: one normalized JSONL format for every benchmark.

Item fields:
  id, suite, db (key resolved by `resolve_db_url`), question, evidence (hint text or null),
  gold_sql (null if unanswerable), gold_answer (text or null), answerable, reference_time,
  split ("dev" | "test" | "reserve"), tags (dict: difficulty, db_id, tie, ...)

Splits are fixed, stratified and seeded, so a question never moves between dev and test.
Tune prompts/tools on dev only; run test once at the end.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
SUITES_DIR = PROJECT / "evals" / "suites"
AGENT_DEMO = PROJECT.parent

MIMIC_DB = Path(os.environ.get("NL2SQL_MIMIC_DB", AGENT_DEMO / "m3_repro" / "db" / "mimic_iv.sqlite"))
MIMIC_DATA = Path(os.environ.get("NL2SQL_MIMIC_DATA", AGENT_DEMO / "m3_repro" / "data"))
BIRD_ROOT = Path(os.environ.get("NL2SQL_BIRD_ROOT", Path.home() / ".local" / "share" / "nl2sql" / "bird"))
BIRD_DATABASES = BIRD_ROOT / "minidev" / "MINIDEV" / "dev_databases"
BIRD_QUESTIONS = BIRD_ROOT / "mini_dev_sqlite_hf.json"  # Hugging Face copy is the canonical version

SEED = 20260915
EHRSQL_NOW = "2100-12-31 23:59:00"

# Stored gold answers that contradict their own gold SQL (found by `nl2sql eval check-gold`).
# For these the stored text is dropped, so the judge compares against the executed gold result.
MIMIC_GOLD_ANSWER_OVERRIDES = {
    "How many days have passed since patient 10039831's last stay in careunit discharge lounge in this hospital visit?":
        "stored 828, gold SQL yields 0.828 days",
}


@dataclass
class EvalItem:
    id: str
    suite: str
    db: str
    question: str
    gold_sql: str | None
    gold_answer: str | None
    answerable: bool
    split: str
    evidence: str | None = None
    reference_time: str | None = None
    tags: dict = field(default_factory=dict)


def resolve_db_url(db_key: str) -> str:
    if db_key == "mimic":
        return f"sqlite:///{MIMIC_DB}"
    if db_key.startswith("bird/"):
        name = db_key.split("/", 1)[1]
        return f"sqlite:///{BIRD_DATABASES / name / f'{name}.sqlite'}"
    return db_key  # already a URL


def _stable_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]


def _stratified_split(items: list[EvalItem], key, fractions: dict[str, float]) -> None:
    """Assign item.split within each stratum, in proportion to `fractions` (remainder -> 'reserve')."""
    strata = defaultdict(list)
    for it in items:
        strata[key(it)].append(it)
    rng = random.Random(SEED)
    for stratum_key in sorted(strata, key=str):
        group = sorted(strata[stratum_key], key=lambda it: it.id)
        rng.shuffle(group)
        n = len(group)
        start = 0
        for split, frac in fractions.items():
            count = round(n * frac)
            for it in group[start:start + count]:
                it.split = split
            start += count
        for it in group[start:]:
            it.split = "reserve"


def build_mimic() -> list[EvalItem]:
    answerable = json.loads((MIMIC_DATA / "answerable.json").read_text())
    unanswerable = json.loads((MIMIC_DATA / "unanswerable.json").read_text())
    tie_questions = {q["query"] for q in json.loads((MIMIC_DATA / "answerable_tiecheck.json").read_text())}

    items = []
    for q in answerable:
        tags = {"tie": q["query"] in tie_questions, "kind": "answerable"}
        gold_answer = q["gold_answer"]
        if q["query"] in MIMIC_GOLD_ANSWER_OVERRIDES:
            tags["gold_answer_override"] = MIMIC_GOLD_ANSWER_OVERRIDES[q["query"]]
            gold_answer = None
        items.append(EvalItem(
            id="mimic-" + _stable_id(q["query"]), suite="mimic", db="mimic", question=q["query"],
            gold_sql=q["gold_sql"], gold_answer=gold_answer, answerable=True, split="",
            reference_time=EHRSQL_NOW, tags=tags,
        ))
    for q in unanswerable:
        items.append(EvalItem(
            id="mimic-" + _stable_id(q["query"]), suite="mimic", db="mimic", question=q["query"],
            gold_sql=None, gold_answer=None, answerable=False, split="",
            reference_time=EHRSQL_NOW, tags={"kind": "unanswerable"},
        ))
    _stratified_split(items, key=lambda it: (it.answerable, it.tags.get("tie", False)), fractions={"dev": 0.6, "test": 0.4})
    return items


def build_bird(dev: int = 150, test: int = 150) -> list[EvalItem]:
    raw = json.loads(BIRD_QUESTIONS.read_text())
    items = [
        EvalItem(
            id=f"bird-{q['question_id']}", suite="bird", db=f"bird/{q['db_id']}", question=q["question"],
            evidence=q.get("evidence") or None, gold_sql=q["SQL"], gold_answer=None, answerable=True,
            split="", tags={"difficulty": q["difficulty"], "db_id": q["db_id"]},
        )
        for q in raw
    ]
    total = len(items)
    _stratified_split(items, key=lambda it: (it.tags["db_id"], it.tags["difficulty"]),
                      fractions={"dev": dev / total, "test": test / total})
    return items


SUITE_BUILDERS = {"mimic": build_mimic, "bird": build_bird}


def prepare(suites: list[str]) -> dict[str, Path]:
    SUITES_DIR.mkdir(parents=True, exist_ok=True)
    written = {}
    for name in suites:
        items = SUITE_BUILDERS[name]()
        path = SUITES_DIR / f"{name}.jsonl"
        path.write_text("".join(json.dumps(asdict(it)) + "\n" for it in items))
        written[name] = path
    return written


def load_suite(name: str, split: str | None = None, limit: int | None = None) -> list[EvalItem]:
    path = SUITES_DIR / f"{name}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing — run `nl2sql eval prepare --suite {name}` first.")
    items = [EvalItem(**json.loads(line)) for line in path.read_text().splitlines() if line.strip()]
    if split:
        items = [it for it in items if it.split == split]
    return items[:limit] if limit else items
