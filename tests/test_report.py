from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from openpyxl import load_workbook

from cleantape.config import Config
from cleantape.report.build import write_report

from broken_scenarios import NOW, SCENARIOS
from test_site import _portfolio, _state_with

DAY = dt.date(2026, 11, 6)


@pytest.fixture(scope="module")
def clean_report(tmp_path_factory: pytest.TempPathFactory, cfg: Config) -> dict[str, Path]:
    tmp = tmp_path_factory.mktemp("rep")
    st, c = _state_with(cfg, tmp)
    return write_report(c, st, DAY, NOW, _portfolio(c, st))


def test_html_is_email_safe(clean_report: dict[str, Path]) -> None:
    html = clean_report["html"].read_text()
    assert "<script" not in html.lower()
    assert "<link" not in html.lower() and "<style" not in html.lower()  # inline CSS only
    assert "<img" not in html.lower() and "<svg" not in html.lower()  # no embedded charts
    assert "Data health: OK" in html and "MOCK" in html
    for section in [
        "Rates",
        "Equities",
        "FX",
        "Commodities",
        "Biggest movers",
        "Treasury curve changes",
        "Portfolio P&amp;L",
        "Data checks",
    ]:
        assert section in html, section
    assert "https://danieldistor.github.io/clean-tape/" in html


def test_excel_has_expected_sheets_and_formats(clean_report: dict[str, Path]) -> None:
    wb = load_workbook(clean_report["xlsx"])
    assert wb.sheetnames == [
        "Summary",
        "Rates",
        "Equities",
        "FX",
        "Commodities",
        "Portfolio",
        "Checks",
    ]
    ws = wb["Rates"]
    assert ws.freeze_panes == "A2"
    assert all(c.font.bold for c in ws[1])
    headers = [c.value for c in ws[1]]
    row = next(r for r in ws.iter_rows(min_row=2) if r[1].value == "DGS10")
    col = {h: i for i, h in enumerate(headers)}
    assert row[col["Last"]].number_format == "0.00%"
    assert row[col["Last"]].value == pytest.approx(0.0484, abs=1e-6)  # 4.84% stored as a fraction
    assert row[col["1d"]].number_format.startswith("+0.0")  # bp
    assert isinstance(row[col["As of"]].value, dt.datetime)  # a real date, not text
    assert ws.page_setup.orientation == "landscape" and ws.page_setup.fitToWidth == 1
    assert ws.conditional_formatting  # green/red + |z| > 2
    eq = wb["Equities"]
    spy = next(r for r in eq.iter_rows(min_row=2) if r[1].value == "SPY")
    assert spy[col["1d"]].number_format == "0.00%"
    assert wb["Rates"].column_dimensions["A"].width > 8  # auto width
    port = wb["Portfolio"]
    assert port.cell(2, 14).number_format.startswith("#,##0")


def test_fail_report_has_red_box_and_marks_numbers(cfg: Config, tmp_path: Path) -> None:
    sc = next(s for s in SCENARIOS if s.name == "yield_250")
    st, c = _state_with(cfg, tmp_path, sc.mutate)
    paths = write_report(c, st, DAY, NOW, _portfolio(c, st))
    html = paths["html"].read_text()
    assert "DATA CHECKS FAILED" in html and "UST 10Y" in html.split("DATA CHECKS FAILED")[1][:300]
    assert "unreliable — failed checks" in html
    assert "250.00%" not in html.split("line-through")[0]  # bad value only appears struck through
    assert "n/a — data issue" in html
    wb = load_workbook(paths["xlsx"])
    assert wb["Summary"]["B4"].value == "FAIL"
    assert wb["Checks"].max_row > 1 and wb["Checks"]["A2"].value == "FAIL"
