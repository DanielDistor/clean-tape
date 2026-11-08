from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import pytest

from cleantape.checks.runner import persist, run_checks
from cleantape.config import Config, load_config
from cleantape.site.build import build_site, fmt_change, fmt_level, sparkline_svg
from cleantape.store.state import State

from broken_scenarios import NOW, SCENARIOS, clean_obs, context
from conftest import ROOT


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        for key in ("href", "src"):
            if a.get(key):
                self.links.append(a[key] or "")
        if a.get("id"):
            self.ids.add(a["id"] or "")


def internal_link_problems(site: Path) -> list[str]:
    """Every relative href/src in every page must resolve to a file in the site."""
    problems = []
    for page in site.rglob("*.html"):
        p = _Links()
        p.feed(page.read_text())
        for link in p.links:
            u = urlparse(link)
            if u.scheme in ("http", "https", "mailto", "data") or link.startswith("#"):
                continue
            if link.startswith("/"):
                problems.append(f"{page.name}: absolute link {link} breaks under /clean-tape/")
                continue
            target = (page.parent / u.path).resolve()
            if not target.exists():
                problems.append(f"{page.relative_to(site)}: broken link {link}")
    return problems


def _state_with(cfg: Config, tmp_path: Path, obs_mutator=None) -> tuple[State, Config]:  # type: ignore[no-untyped-def]
    c = load_config(ROOT, state_dir=tmp_path / "state")
    st = State(c.state_dir)
    obs = clean_obs() if obs_mutator is None else obs_mutator(clean_obs())
    ctx = context(c, obs)
    st.write("observations", obs.drop_duplicates(["date", "instrument_id", "source"]))
    st.write("derived", ctx.derived)
    persist(st, run_checks(ctx))
    rep = st.root / "reports" / "2026-11-06"
    rep.mkdir(parents=True)
    (rep / "eod_2026-11-06.html").write_text("<html></html>")
    (rep / "eod_2026-11-06.xlsx").write_bytes(b"x")
    return st, c


def _portfolio(c: Config, st: State):  # type: ignore[no-untyped-def]
    from cleantape.portfolio import pnl

    positions = pnl.load_positions(c.path(c.settings.paths.portfolio), c.universe)
    wide = pnl.wide_values(st.read("derived"))
    as_of = pnl.expected_dates(c, positions, NOW)
    checks = st.read("check_results")
    flagged = set(checks[checks["severity"] == "FAIL"]["subject"])
    pos = pnl.value_positions(positions, wide, c.universe, as_of, flagged)
    return {
        "positions": pos,
        "by_book": pnl.aggregate(pos, "book"),
        "by_asset_class": pnl.aggregate(pos, "asset_class"),
        "totals": pnl.totals(pos),
        "shocks": pnl.shocks_table(pos, c.settings.shocks),
        "history": pnl.pnl_history(positions, wide, c.universe, sorted(wide.index)[-10:]),
    }


@pytest.fixture(scope="module")
def clean_site(tmp_path_factory: pytest.TempPathFactory, cfg: Config) -> Path:
    tmp = tmp_path_factory.mktemp("clean")
    st, c = _state_with(cfg, tmp)
    info = build_site(c, st, NOW, _portfolio(c, st), out_dir=tmp / "site")
    assert info["status"] == "OK"
    return tmp / "site"


def test_all_pages_exist(clean_site: Path) -> None:
    for name in ["index", "rates", "markets", "portfolio", "health", "reports", "about"]:
        assert (clean_site / f"{name}.html").exists()


def test_every_internal_link_resolves(clean_site: Path) -> None:
    assert internal_link_problems(clean_site) == []


def test_series_json_is_valid_and_compact(clean_site: Path) -> None:
    files = list((clean_site / "data" / "series").glob("*.json"))
    assert len(files) > 30
    s = json.loads((clean_site / "data" / "series" / "DGS10.json").read_text())
    assert s["units"] == "pct_yield" and len(s["d"]) == len(s["v"]) == len(s["m"])
    assert all(v is None or len(str(v)) <= 8 for v in s["v"])  # rounded


def test_markets_options_have_json(clean_site: Path) -> None:
    html = (clean_site / "markets.html").read_text()
    for sid in re.findall(r'<option value="([A-Z0-9_]+)"', html):
        assert (clean_site / "data" / "series" / f"{sid}.json").exists(), sid


def test_overview_has_mock_label_and_no_fail_banner(clean_site: Path) -> None:
    html = (clean_site / "index.html").read_text()
    assert "MOCK" in html and "DATA CHECKS FAILED" not in html
    assert "PIPELINE MAY BE DOWN" not in html
    assert '<svg class="spark"' in html


def test_static_assets_are_cache_busted(clean_site: Path) -> None:
    html = (clean_site / "index.html").read_text()
    assert re.search(r'href="static/style\.css\?v=[0-9a-f]{10}"', html)
    assert re.search(r'src="static/app\.js\?v=[0-9a-f]{10}"', html)


def test_reports_archive_links(clean_site: Path) -> None:
    html = (clean_site / "reports.html").read_text()
    assert 'href="reports/2026-11-06/eod_2026-11-06.html"' in html
    assert (clean_site / "reports" / "2026-11-06" / "eod_2026-11-06.xlsx").exists()


def test_fail_is_visible_and_numbers_are_struck(cfg: Config, tmp_path: Path) -> None:
    sc = next(s for s in SCENARIOS if s.name == "yield_250")
    st, c = _state_with(cfg, tmp_path, sc.mutate)
    build_site(c, st, NOW, _portfolio(c, st), out_dir=tmp_path / "site")
    html = (tmp_path / "site" / "index.html").read_text()
    assert "DATA CHECKS FAILED" in html and "UST 10Y" in html.split("Unreliable:")[1][:400]
    row = re.search(r'<tr id="DGS10"[^>]*>.*?</tr>', html, re.S)
    assert row and 'class="row-fail"' in row.group(0) and "<s>" in row.group(0)
    assert "<svg" not in row.group(0)  # no sparkline drawing the bad print
    # derived spreads built on the bad leg are marked too
    s2s10s = re.search(r'<tr id="S2S10S"[^>]*>.*?</tr>', html, re.S)
    assert s2s10s and "<s>" in s2s10s.group(0)
    movers = html.split('aria-label="Biggest moves by z-score"')[1].split("</section>")[0]
    assert "UST 10Y</span>" not in movers and "Excluded because their data FAILED" in movers
    tape = html.split('class="tape"')[1].split("</header>")[0]
    assert 'UST 10Y</span> <span class="bad">✕ UNRELIABLE' in tape and "250.00%" not in tape
    port = (tmp_path / "site" / "portfolio.html").read_text()
    assert "n/a — data issue" in port
    assert "light-fail" in html


def test_stale_banner_when_data_old(cfg: Config, tmp_path: Path) -> None:
    import datetime as dt

    st, c = _state_with(cfg, tmp_path)
    later = NOW + dt.timedelta(days=7)
    build_site(c, st, later, None, out_dir=tmp_path / "site")
    assert "PIPELINE MAY BE DOWN" in (tmp_path / "site" / "index.html").read_text()


def test_empty_state_still_builds_red(cfg: Config, tmp_path: Path) -> None:
    c = load_config(ROOT, state_dir=tmp_path / "state")
    info = build_site(c, State(c.state_dir), NOW, None, out_dir=tmp_path / "site")
    assert info["status"] == "FAIL"
    assert internal_link_problems(tmp_path / "site") == []


def test_formatters() -> None:
    assert fmt_level(4.8412, "pct_yield") == "4.84%"
    assert fmt_level(157.4981, "fx_rate", "USDJPY") == "157.498"
    assert fmt_level(1.12283, "fx_rate", "EURUSD") == "1.1228"
    assert fmt_level(4074.9, "price") == "4,074.9"
    assert fmt_level(float("nan"), "price") == "—"
    assert fmt_change(6.0, "bp") == "+6.0bp" and fmt_change(-0.314, "pct") == "-0.31%"
    assert sparkline_svg([1.0]) == "" and "<polyline" in sparkline_svg([1.0, 2.0, 1.5])


def test_check_history_groups_by_new_york_date() -> None:
    import pandas as pd

    from cleantape.site.build import check_history

    checks = pd.DataFrame(
        {
            "run_id": [
                "20261108T013000Z",
                "20261108T013000Z",
                "20261108T120000Z",
            ],  # 20:30 ET Nov 7
            "severity": ["WARN", "OK", "FAIL"],
        }
    )
    h = check_history(checks)
    assert h["d"] == ["2026-11-07", "2026-11-08"]
    assert h["warn"] == [1, 0] and h["fail"] == [0, 1]


def test_failure_demo_is_red_and_links_resolve(cfg: Config, tmp_path: Path) -> None:
    from cleantape.site.demo import build_failure_demo

    st, c = _state_with(cfg, tmp_path)
    site = tmp_path / "site"
    build_site(c, st, NOW, _portfolio(c, st), out_dir=site)  # builds site/demo too
    assert (site / "demo" / "index.html").exists()
    assert 'href="demo/index.html"' in (site / "about.html").read_text()
    info = build_failure_demo(c, st, NOW, tmp_path / "demo2")
    assert info is not None and info["status"] == "FAIL"
    html = (site / "demo" / "index.html").read_text()
    assert "banner-demo" in html and "DATA CHECKS FAILED" in html and "<s>250.00%</s>" in html
    assert internal_link_problems(site) == []  # covers the demo pages and the back-link
    # the real state is untouched
    obs = st.read("observations")
    assert obs[obs["instrument_id"] == "DGS10"]["value"].max() < 20
