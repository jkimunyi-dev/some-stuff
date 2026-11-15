"""Agent plumbing tests with NO model: scorer, safe eval, budget ledger (mock), tool runner reproducing Train references."""
import os
from pathlib import Path

import pytest

from qa.agent import Runner, ToolError, digest, safe_eval, _extract_json
from qa.llm import LLM, BudgetExceeded, Ledger, MAX_CALLS
from qa.score import correctness, numbers, score_items

SRC = Path(os.environ.get("QA_SOURCES", ""))
needs_src = pytest.mark.skipif(not SRC.is_dir(), reason="QA_SOURCES not set")


def f(name):
    return next(SRC.glob(f"SRC_*__{name}"))


def test_numbers_skip_years_ids_keep_decimals():
    assert numbers("From 2023 to 2024 it rose 1,964.06 GWh or 8.07% (TPO453, 35 days)") == [1964.06, 8.07, 35.0]
    assert numbers("fell by -3.5 pp") == [-3.5]


def test_correctness_levels():
    ref = "rose 1,964.06 GWh, or 8.07%"
    assert correctness(ref, "It rose by 1964.06 GWh (8.07 percent)") == 1.0
    assert correctness(ref, "It rose 1964.06 GWh") == 0.5
    assert correctness(ref, "It rose 8.1%") == 0.0
    assert correctness("no figures here", "x") is None


def test_score_items_equal_domain_weight():
    rows = [dict(domain="a", reference="1.00", answer="1.00", evidence_ref=["f.csv, c"], evidence=["f.csv x"]),
            dict(domain="b", reference="1.00", answer="9.00", evidence_ref=["g.csv"], evidence=["g.csv"]),
            dict(domain="b", reference="2.00", answer="2.00", evidence_ref=["g.csv"], evidence=[])]
    s = score_items(rows)
    assert s["by_domain"]["a"] == 1.0 and s["by_domain"]["b"] == pytest.approx(0.4)
    assert s["overall"] == pytest.approx(0.7)


def test_safe_eval_blocks_escape():
    assert safe_eval("pct_change(a, b)", {"a": 100, "b": 110}) == pytest.approx(10)
    for bad in ("__import__('os')", "a.__class__", "open('x')", "[x for x in range(3)]"):
        with pytest.raises(Exception):
            safe_eval(bad, {"a": 1})


def test_extract_json_tolerates_fences_and_prose():
    assert _extract_json('sure ```json\n{"steps":[{"id":"a"}]}\n``` done') == {"steps": [{"id": "a"}]}
    with pytest.raises(ValueError):
        _extract_json("no json")


def test_budget_ledger_hard_stop(monkeypatch):
    llm = LLM()
    led = Ledger()

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b'{"message":{"content":"x"},"eval_count":1000,"prompt_eval_count":5}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: R())
    for _ in range(12):
        llm.chat([{"role": "user", "content": "hi"}], led)
    assert led.tokens == 12_000
    with pytest.raises(BudgetExceeded):
        llm.chat([{"role": "user", "content": "hi"}], led)
    led2 = Ledger()
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: R())
    led2.calls = [{"eval_tokens": 0, "seconds": 0}] * MAX_CALLS
    with pytest.raises(BudgetExceeded):
        llm.chat([{"role": "user", "content": "hi"}], led2)


@needs_src
def test_runner_reproduces_train_helsinki():
    files = [f("Paakaupunkiseudun_energiankulutus_1990_ja_2000-2024.xlsx")]
    plan = {"steps": [{"id": "a", "tool": "cell", "args": {"file": "Paakaupunkiseudun_energiankulutus_1990_ja_2000-2024.xlsx", "sheet": "Tulokset", "ref": "Z9"}},
                      {"id": "b", "tool": "cell", "args": {"file": "Paakaupunkiseudun_energiankulutus_1990_ja_2000-2024.xlsx", "sheet": "Tulokset", "ref": "AA9"}},
                      {"id": "r", "tool": "calc", "args": {"expr": "pct_change(a, b)"}}], "final": "r"}
    rn = Runner(files, "Use the stored numeric cell values")
    v = rn.run(plan)
    assert round(v["r"], 2) == 8.07 and len(rn.rec.items) == 2


@needs_src
def test_runner_tpo_days_and_errors():
    plan = {"steps": [{"id": "m", "tool": "csv_value", "args": {"file": "trees-dataset-2026-06-23.csv", "where_col": "reference", "where_val": "TPO453", "col": "made_date"}},
                      {"id": "c", "tool": "csv_value", "args": {"file": "trees-dataset-2026-06-23.csv", "where_col": "reference", "where_val": "TPO453", "col": "confirmed_date"}},
                      {"id": "d", "tool": "calc", "args": {"expr": "days(m, c)"}}], "final": "d"}
    assert Runner([f("trees-dataset-2026-06-23.csv")], "").run(plan)["d"] == 35
    bad = {"steps": [{"id": "x", "tool": "csv_value", "args": {"file": "trees-dataset-2026-06-23.csv", "where_col": "reference", "where_val": "NOPE", "col": "made_date"}}]}
    with pytest.raises(ToolError):
        Runner([f("trees-dataset-2026-06-23.csv")], "").run(bad)
    with pytest.raises(ToolError):
        Runner([], "").run({"steps": [{"id": "x", "tool": "nope", "args": {}}]})


@needs_src
def test_runner_zone_area_ha():
    plan = {"steps": [{"id": "p", "tool": "polygon_area_ha", "args": {"file": "zones-2026-06-23.geojson", "id_field": "reference", "id_value": "1912/A1", "crs": "EPSG:27700"}}], "final": "p"}
    assert round(Runner([f("zones-2026-06-23.geojson")], "").run(plan)["p"], 2) == 3.48


@needs_src
def test_digest_shows_addresses_and_year_map():
    d = digest(f("Paakaupunkiseudun_energiankulutus_1990_ja_2000-2024.xlsx"))
    assert "A2=" in d and "year->column" in d and "2024:AA" in d
    assert "BUS01a" in digest(f("bus01.ods"))
