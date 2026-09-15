from decimal import Decimal

from nl2sql.eval.compare import compare_results
from nl2sql.eval.datasets import EvalItem
from nl2sql.eval.scoring import Scorer, gold_sql_for_execution


def test_order_and_duplicates_ignored():
    assert compare_results([(1, "a"), (2, "b")], [(2, "b"), (1, "a"), (1, "a")]).match


def test_numeric_normalization():
    assert compare_results([(24.98333333,)], [(24.98,)]).match
    assert compare_results([(Decimal("523.06"),)], [(523.06,)]).match
    assert compare_results([(14,)], [(14.0,)]).match
    assert compare_results([("14",)], [(14,)]).match
    assert not compare_results([(0.0123,)], [(0.0129,)]).match


def test_string_normalization():
    assert compare_results([("Led Zeppelin",)], [(" led zeppelin ",)]).match
    assert not compare_results([("Led Zeppelin",)], [("Dread Zeppelin",)]).match


def test_projection_allows_extra_columns_in_any_order():
    gold = [("Rock",), ("Jazz",)]
    pred = [(1, "Jazz", 30), (2, "Rock", 12)]
    m = compare_results(gold, pred)
    assert m.match and m.rule == "projection"


def test_projection_needs_all_rows():
    assert not compare_results([("Rock",), ("Jazz",)], [(1, "Rock")]).match


def test_missing_or_extra_rows_fail():
    assert not compare_results([(1,), (2,), (3,)], [(1,), (2,)]).match
    assert not compare_results([(1,), (2,)], [(1,), (2,), (3,)]).match


def test_empty_results():
    assert compare_results([], []).match
    assert not compare_results([(1,)], []).match


def test_current_time_substitution():
    item = EvalItem(id="x", suite="mimic", db="mimic", question="q", gold_answer=None, answerable=True, split="dev",
                    gold_sql="SELECT datetime(current_time,'start of year') , 'current_time_col'", reference_time="2100-12-31 23:59:00")
    sql = gold_sql_for_execution(item)
    assert "datetime('2100-12-31 23:59:00','start of year')" in sql
    assert "'current_time_col'" in sql  # word boundary: identifiers containing it are untouched


def test_unanswerable_scoring_needs_no_database():
    item = EvalItem(id="u", suite="mimic", db="mimic", question="q", gold_sql=None, gold_answer=None,
                    answerable=False, split="dev")
    scorer = Scorer(judge=False)
    assert scorer.score(item, {"status": "unanswerable"}, {})["correct"] is True
    wrong = scorer.score(item, {"status": "answered", "sql": "SELECT 1"}, {})
    assert wrong["correct"] is False and wrong["error_type"] == "false_answer"


def test_mcnemar_exact():
    from nl2sql.eval.compare_runs import mcnemar_exact
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(5, 5) == 1.0
    assert abs(mcnemar_exact(10, 0) - 2 / 1024) < 1e-12
    assert 0.03 < mcnemar_exact(12, 3) < 0.04  # 2 * P(X<=3), X~Bin(15, .5) = 0.0351


def test_deterministic_error_labels():
    from nl2sql.eval.labeling import deterministic_label, needs_label
    item = EvalItem(id="a", suite="bird", db="bird/x", question="q", gold_sql="SELECT 1", gold_answer=None,
                    answerable=True, split="dev")
    failed = {"status": "failed", "score": {"correct": False, "rule": "agent_failed"}}
    abstained = {"status": "unanswerable", "score": {"correct": False, "rule": "false_abstention"}}
    wrong = {"status": "answered", "score": {"correct": False, "rule": "none"}}
    right = {"status": "answered", "score": {"correct": True, "rule": "exact"}}
    excluded = {"status": "answered", "score": {"correct": None, "rule": "gold_error"}}
    assert deterministic_label(item, failed) == "no_usable_answer"
    assert deterministic_label(item, abstained) == "false_abstention"
    assert deterministic_label(item, wrong) is None
    assert needs_label(item, wrong) and not needs_label(item, right) and not needs_label(item, excluded)
