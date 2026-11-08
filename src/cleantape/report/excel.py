"""Excel version of the EOD report, formatted the way an analyst would.

Sheets: Summary, Rates, Equities, FX, Commodities, Portfolio, Checks. Frozen header row, bold
headers, real number formats (yields/% as 0.00%, bp as 0.0, prices with sensible decimals,
dates as dates), green/red on changes, |z| > 2 highlighted, auto widths, landscape + fit to width.
Values are written as numbers (not text) so the workbook stays usable for further analysis.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from cleantape.report.build import SHEETS, ReportModel, ReportRow

BOLD = Font(bold=True)
HEADER_FILL = PatternFill("solid", fgColor="E1E0D9")
FAIL_FILL = PatternFill("solid", fgColor="FDE8E8")
WARN_FILL = PatternFill("solid", fgColor="FFF6DC")
HOT_FILL = PatternFill("solid", fgColor="FFF1C7")
GREEN = Font(color="006300")
RED = Font(color="C22F2F")

FMT_DATE = "yyyy-mm-dd"
FMT_PCT = "0.00%"
FMT_BP = "+0.0;-0.0;0.0"
FMT_USD = "#,##0;[Red]-#,##0"
FMT_Z = "+0.0;-0.0;0.0"


def _num(v: float | None) -> float | None:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def level_format(units: str, series_id: str) -> str:
    if units == "pct_yield":
        return FMT_PCT
    if units == "bp_spread":
        return "0"
    if units == "fx_rate":
        return "0.000" if "JPY" in series_id else "0.0000"
    return "#,##0.00"


def excel_level(row: ReportRow) -> float | None:
    """Yields are stored in percent (4.84) → Excel percent fraction (0.0484)."""
    v = _num(row.value)
    return v / 100 if v is not None and row.units == "pct_yield" else v


def excel_change(v: float | None, mode: str) -> float | None:
    """bp stays bp (0.0 format); % changes become fractions for the 0.00% format."""
    x = _num(v)
    return x if x is None or mode == "bp" else x / 100


def _header(ws: Worksheet, headers: list[str]) -> None:
    ws.append(headers)
    for c in ws[1]:
        c.font = BOLD
        c.fill = HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"


def _finish(ws: Worksheet, min_width: int = 8, max_width: int = 60) -> None:
    for i, col in enumerate(ws.iter_cols(min_row=1, max_row=ws.max_row), start=1):
        width = max((len(str(c.value)) for c in col if c.value is not None), default=0)
        ws.column_dimensions[get_column_letter(i)].width = max(min_width, min(max_width, width + 2))
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "1:1"
    if ws.max_row > 1:
        ws.auto_filter.ref = ws.dimensions


def _sign_colors(ws: Worksheet, rng: str) -> None:
    ws.conditional_formatting.add(
        rng, CellIsRule(operator="greaterThan", formula=["0"], font=GREEN)
    )
    ws.conditional_formatting.add(rng, CellIsRule(operator="lessThan", formula=["0"], font=RED))


def market_sheet(ws: Worksheet, rows: list[ReportRow]) -> None:
    """One instrument per row: level, 1d/1w/1m/YTD change, z, as-of, status."""
    _header(
        ws,
        [
            "Series",
            "ID",
            "As of",
            "Last",
            "Change unit",
            "1d",
            "1w",
            "1m",
            "YTD",
            "z (1d vs 1y)",
            "Source",
            "Data status",
        ],
    )
    for r in rows:
        unit = "bp" if r.mode == "bp" else "%"
        ws.append(
            [
                r.name,
                r.series_id,
                r.date,
                excel_level(r),
                unit,
                excel_change(r.chg_1d, r.mode),
                excel_change(r.chg_1w, r.mode),
                excel_change(r.chg_1m, r.mode),
                excel_change(r.chg_ytd, r.mode),
                _num(r.z),
                r.source,
                r.status if r.flag is None else f"{r.status}: {r.flag.tooltip}",
            ]
        )
        i = ws.max_row
        ws.cell(i, 3).number_format = FMT_DATE
        ws.cell(i, 4).number_format = level_format(r.units, r.series_id)
        for col in range(6, 10):
            ws.cell(i, col).number_format = FMT_BP if r.mode == "bp" else FMT_PCT
        ws.cell(i, 10).number_format = FMT_Z
        if r.unreliable:
            for c in ws[i]:
                c.fill = FAIL_FILL
            ws.cell(i, 4).font = Font(strike=True, color="52514E")
        elif r.flag:
            ws.cell(i, 12).fill = WARN_FILL
    n = ws.max_row
    if n > 1:
        _sign_colors(ws, f"F2:I{n}")
        ws.conditional_formatting.add(
            f"J2:J{n}",
            FormulaRule(formula=["AND(ISNUMBER(J2),ABS(J2)>2)"], fill=HOT_FILL, font=BOLD),
        )
    _finish(ws)


def summary_sheet(ws: Worksheet, m: ReportModel) -> None:
    ws.append(["Clean Tape — end-of-day report"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append(["As of", m.date])
    ws["B2"].number_format = FMT_DATE
    ws.append(["Generated (UTC)", m.generated_utc.replace(tzinfo=None)])
    ws["B3"].number_format = "yyyy-mm-dd hh:mm"
    ws.append(["Data health", m.status])
    ws["B4"].font = Font(
        bold=True, color={"OK": "006300", "WARN": "9A6700", "FAIL": "D03B3B"}[m.status]
    )
    ws.append(["Unreliable (failed checks)", ", ".join(m.unreliable) or "none"])
    ws.append(["Live site", m.site_url])
    if m.portfolio:
        t = m.portfolio["totals"]
        ws.append(["MOCK portfolio P&L (USD)", _num(t["pnl_usd"])])
        ws.cell(ws.max_row, 2).number_format = FMT_USD
        ws.append(["Positions n/a (data issue)", t["n_a"]])
    ws.append([])
    ws.append(["Biggest movers by |z|", "As of", "1d", "Unit", "z"])
    hdr = ws.max_row
    for c in ws[hdr]:
        c.font = BOLD
        c.fill = HEADER_FILL
    for r in m.movers:
        ws.append(
            [
                r.name,
                r.date,
                excel_change(r.chg_1d, r.mode),
                "bp" if r.mode == "bp" else "%",
                _num(r.z),
            ]
        )
        i = ws.max_row
        ws.cell(i, 2).number_format = FMT_DATE
        ws.cell(i, 3).number_format = FMT_BP if r.mode == "bp" else FMT_PCT
        ws.cell(i, 5).number_format = FMT_Z
    if ws.max_row > hdr:
        _sign_colors(ws, f"C{hdr + 1}:C{ws.max_row}")
    for c in ws["A"][:8]:
        c.font = BOLD
    _finish(ws, max_width=50)
    ws.freeze_panes = None


def portfolio_sheet(ws: Worksheet, p: dict[str, Any] | None) -> None:
    _header(
        ws,
        [
            "Position",
            "Instrument",
            "Book",
            "Asset class",
            "Kind",
            "Quantity",
            "DV01 (USD)",
            "As of",
            "Prev date",
            "Value t",
            "Value t-1",
            "Move",
            "Move unit",
            "P&L (USD)",
            "Exposure (USD)",
            "Status",
            "Note (MOCK)",
        ],
    )
    if not p:
        ws.append(["portfolio could not be valued"])
        _finish(ws)
        return
    for r in p["positions"].to_dict("records"):
        ws.append(
            [
                r["position_id"],
                r["instrument_id"],
                r["book"],
                r["asset_class"],
                r["kind"],
                _num(r["quantity"]),
                _num(r["dv01_usd"]),
                r["as_of"],
                r["prev_date"],
                _num(r["value_t"]),
                _num(r["value_prev"]),
                _num(r["move"]),
                r["move_unit"],
                _num(r["pnl_usd"]) if r["status"] == "ok" else "n/a — data issue",
                _num(r["exposure_usd"]),
                r["status"] if r["status"] == "ok" else r["reason"],
                r["note"],
            ]
        )
        i = ws.max_row
        for col in (8, 9):
            ws.cell(i, col).number_format = FMT_DATE
        for col in (6, 7, 14, 15):
            ws.cell(i, col).number_format = FMT_USD
        ws.cell(i, 12).number_format = "+0.00;-0.00"
        if r["status"] != "ok":
            for c in ws[i]:
                c.fill = FAIL_FILL
    n = ws.max_row
    ws.append([])
    ws.append(["Book", "P&L (USD)", "Gross (USD)", "Net (USD)", "DV01 (USD)", "n/a"])
    for c in ws[ws.max_row]:
        c.font = BOLD
        c.fill = HEADER_FILL
    for b in p["by_book"].to_dict("records"):
        ws.append(
            [
                b["book"],
                _num(b["pnl_usd"]),
                b["gross_exposure_usd"],
                b["net_exposure_usd"],
                b["dv01_usd"],
                b["n_a"],
            ]
        )
        for col in range(2, 6):
            ws.cell(ws.max_row, col).number_format = FMT_USD
    ws.append([])
    ws.append(["Shock scenario (linear)", "P&L (USD)"])
    for c in ws[ws.max_row]:
        c.font = BOLD
        c.fill = HEADER_FILL
    for s in p["shocks"].to_dict("records"):
        ws.append([s["scenario"], s["pnl_usd"]])
        ws.cell(ws.max_row, 2).number_format = FMT_USD
    _sign_colors(ws, f"N2:N{n}")
    _finish(ws, max_width=40)
    ws.auto_filter.ref = f"A1:Q{n}"


def checks_sheet(ws: Worksheet, m: ReportModel) -> None:
    _header(ws, ["Severity", "Check", "Subject", "As of", "Message", "Observed", "Expected"])
    for c in m.checks.to_dict("records"):
        ws.append(
            [
                c["severity"],
                c["check"],
                c["subject"],
                c["as_of_date"],
                c["message"],
                c["observed"],
                c["expected"],
            ]
        )
        i = ws.max_row
        ws.cell(i, 4).number_format = FMT_DATE
        ws.cell(i, 1).fill = FAIL_FILL if c["severity"] == "FAIL" else WARN_FILL
        ws.cell(i, 1).font = BOLD
    if ws.max_row == 1:
        ws.append(["OK", "all", "", m.date, "No WARN or FAIL results", "", ""])
        ws.cell(2, 4).number_format = FMT_DATE
    _finish(ws, max_width=90)


def write_workbook(m: ReportModel, path: Path) -> None:
    wb = Workbook()
    summary_sheet(wb.active, m)
    wb.active.title = "Summary"
    for name in SHEETS:
        market_sheet(wb.create_sheet(name), m.sections.get(name, []))
    portfolio_sheet(wb.create_sheet("Portfolio"), m.portfolio)
    checks_sheet(wb.create_sheet("Checks"), m)
    wb.save(path)
