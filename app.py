import base64
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

from massive_api import MassiveApiError, get_futures_curve, get_settlement_histories

HERE = Path(__file__).parent

LOGO_FILE = "logo-50yr.png"
FAVICON_FILE = "jsa_favicon.png"
WATERMARK_FILE = "logo-50yr.png"
WATERMARK_OPACITY = 0.10


def asset(name: str) -> str:
    return str(HERE / "assets" / name)


def watermark_path() -> str | None:
    for name in (WATERMARK_FILE, LOGO_FILE):
        candidate = asset(name)
        if Path(candidate).exists():
            return candidate
    return None


@st.cache_data(show_spinner=False)
def watermark_uri(path: str) -> str:
    return "data:image/png;base64," + base64.b64encode(Path(path).read_bytes()).decode()


st.set_page_config(
    page_title="JSA - Livestock Seasonal Futures & Spreads",
    page_icon=asset(FAVICON_FILE),
    layout="wide",
)

st.markdown("""<style>
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header {visibility: hidden;}
</style>""", unsafe_allow_html=True)

# Hide the Streamlit Community Cloud viewer badge (the profile avatar that links
# to the creator's other apps) for a clean, client-facing footer.
# The Streamlit Cloud badge (creator avatar + logo) is drawn by Cloud's outer page,
# outside this iframe, so CSS here can't reach it; add the rule to the parent
# document instead (same-origin). No-op when run locally.
_BADGE_JS = """<script>(function(){try{var w=window;while(w.parent&&w.parent!==w){try{void w.parent.document;w=w.parent;}catch(e){break;}}var d=w.document;if(d.getElementById('jsa-hide-cloud-badge'))return;var s=d.createElement('style');s.id='jsa-hide-cloud-badge';s.textContent="[class*='_profileContainer_'],[class*='_viewerBadge_']{display:none !important;}";d.head.appendChild(s);}catch(e){}})();</script>"""
try:
    st.html(_BADGE_JS, unsafe_allow_javascript=True)
except TypeError:  # older Streamlit without st.html JS support
    import streamlit.components.v1 as _stc
    _stc.html(_BADGE_JS, height=0)

COMMODITIES = [
    {"key": "live_cattle", "label": "Live Cattle", "sublabel": "CME · LE", "product_code": "LE", "unit": "¢/lb"},
    {"key": "feeder_cattle", "label": "Feeder Cattle", "sublabel": "CME · GF", "product_code": "GF", "unit": "¢/lb"},
    {"key": "lean_hogs", "label": "Lean Hogs", "sublabel": "CME · HE", "product_code": "HE", "unit": "¢/lb"},
]
COMMODITY_BY_CODE = {c["product_code"]: c for c in COMMODITIES}

MONTH_LETTERS = {
    "F": "Jan", "G": "Feb", "H": "Mar", "J": "Apr", "K": "May", "M": "Jun",
    "N": "Jul", "Q": "Aug", "U": "Sep", "V": "Oct", "X": "Nov", "Z": "Dec",
}

# Standard CME contract months per product, confirmed from a live /contracts pull.
STANDARD_MONTHS = {"LE": "GJMQVZ", "GF": "FHJKQUVX", "HE": "GJKMNQVZ"}
CONTINUOUS_YEARS_BACK = 6
MONTH_ORDER = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

YEAR_COLORS = ["#0693e3", "#e8833a", "#5aa469", "#b05fb0", "#9aa5b1", "#c0392b"]
AVG_COLOR = "#111111"
EXP_COLOR = "#c62828"
FND_COLOR = "#8e24aa"
UP_COLOR = "#16a34a"
DOWN_COLOR = "#dc2626"
MAX_YEARS_BACK = 5
DATA_START_NOTE = (
    "Massive's daily settlement history starts 2021-09-02, so seasonal overlays "
    "cover roughly the last 4 full contract years — older analogs are skipped, not wrong."
)
FND_NOTE = (
    "FND marks CME's Live Cattle First Notice Day — the first Monday following the "
    "first Friday of the contract month (Rulebook Ch. 101). Feeder Cattle and Lean "
    "Hogs are cash-settled with no physical delivery, so no FND applies to them."
)

GROUP_BAND = "background-color:#EAF7EA;"


def live_cattle_fnd(delivery_month: date) -> date:
    """CME Live Cattle First Notice Day: the first Monday following the first
    Friday of the delivery (contract) month. Only Live Cattle (LE) is
    physically delivered — Feeder Cattle and Lean Hogs are cash-settled
    against an index and have no FND."""
    first_of_month = pd.Timestamp(year=delivery_month.year, month=delivery_month.month, day=1)
    first_friday = first_of_month + pd.Timedelta(days=(4 - first_of_month.weekday()) % 7)
    return (first_friday + pd.Timedelta(days=3)).date()


# USDA's ESMIS system (esmis.nal.usda.gov) is a public, no-key catalog of every
# agency report with an exact release_datetime per issue — more authoritative
# for "when was this report released" than reverse-engineering it from the
# NASS QuickStats/WASDE data feeds themselves, so report-date markers are
# sourced from here rather than from NASS_API_KEY or a WASDE data key.
ESMIS_BASE = "https://esmis.nal.usda.gov/api/v1"
REPORT_PUBLICATIONS = {
    "Cattle on Feed": 2270,
    "Hogs and Pigs": 1474,
    "Livestock Slaughter": 2233,
    "WASDE": 1659,
}
# Which reports move which commodity: Hogs and Pigs is hog-specific; Cattle on
# Feed drives both fed-cattle supply and feeder demand; Livestock Slaughter
# covers cattle and hogs (not a separate feeder cattle count); WASDE's meat
# production/outlook tables touch all three loosely.
REPORT_RELEVANCE = {
    "LE": ["Cattle on Feed", "Livestock Slaughter", "WASDE"],
    "GF": ["Cattle on Feed", "WASDE"],
    "HE": ["Hogs and Pigs", "Livestock Slaughter", "WASDE"],
}
REPORT_COLORS = {
    "Cattle on Feed": "#795548",
    "Hogs and Pigs": "#ff9800",
    "Livestock Slaughter": "#009688",
    "WASDE": "#607d8b",
}


@st.cache_data(ttl="24h", show_spinner=False)
def fetch_report_dates(report: str, since: str) -> list[date]:
    """Release dates for a USDA report since a cutoff date, newest-first
    pagination stopped as soon as a page is entirely before the cutoff —
    typically 1-4 requests rather than the report's full multi-decade history."""
    pub_id = REPORT_PUBLICATIONS[report]
    since_date = date.fromisoformat(since)
    dates: list[date] = []
    page = 0
    while page < 40:
        try:
            resp = requests.get(f"{ESMIS_BASE}/release/findByPubId/{pub_id}",
                                params={"page": page}, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException:
            break
        results = data.get("results", [])
        if not results:
            break
        any_recent = False
        for r in results:
            dt_str = r.get("release_datetime")
            if not dt_str:
                continue
            d = pd.to_datetime(dt_str).date()
            if d >= since_date:
                dates.append(d)
                any_recent = True
        if not any_recent:
            break
        page += 1
        if page >= data.get("pager", {}).get("total_pages", page + 1):
            break
    return sorted(set(dates))


REPORT_COLOR_NAMES = {
    "Cattle on Feed": "brown",
    "Hogs and Pigs": "orange",
    "Livestock Slaughter": "teal",
    "WASDE": "blue-grey",
}


def relevant_report_dates(code: str, as_of: date) -> dict[str, list[date]]:
    """Report dates for the commodities that report moves, over a fixed 2-year
    lookback (cached independent of the chart's own window selector so
    switching 6M/1Y/18M doesn't refetch)."""
    since = (as_of - timedelta(days=730)).isoformat()
    return {r: fetch_report_dates(r, since) for r in REPORT_RELEVANCE.get(code, [])}


def add_report_vlines(fig, dates_by_report: dict[str, list[date]], start: date, end: date):
    """Thin, unlabeled dotted markers — a report's release date, not a price
    level, so no annotation text is drawn to keep the chart from getting
    cluttered by four report types' worth of monthly/quarterly dates."""
    for report, dates in dates_by_report.items():
        color = REPORT_COLORS[report]
        for d in dates:
            if start <= d <= end:
                fig.add_shape(type="line", xref="x", yref="paper", x0=d, x1=d, y0=0, y1=1,
                              line=dict(color=color, dash="dot", width=1), opacity=0.55)


def report_legend_caption(code: str) -> str:
    parts = [f"{r} ({REPORT_COLOR_NAMES[r]})" for r in REPORT_RELEVANCE.get(code, [])]
    return "Dotted vertical lines mark USDA report release dates: " + ", ".join(parts) + "."


def get_api_key() -> str:
    try:
        key = st.secrets.get("MASSIVE_API_KEY", "")
    except Exception:
        key = ""
    return key or os.environ.get("MASSIVE_API_KEY", "")


def friendly_contract(ticker: str, product_code: str) -> str:
    suffix = ticker[len(product_code):]
    if len(suffix) == 2 and suffix[0] in MONTH_LETTERS:
        return f"{MONTH_LETTERS[suffix[0]]} '2{suffix[1]}"
    return ticker


def shift_ticker_year(ticker: str, product_code: str, delta: int) -> str | None:
    """LEZ6 -> LEZ5 at delta=-1. Massive quotes outrights with a single-digit year."""
    suffix = ticker[len(product_code):]
    if len(suffix) != 2 or suffix[0] not in MONTH_LETTERS:
        return None
    month, year = suffix[0], int(suffix[1])
    shifted = year + delta
    if shifted < 0:
        return None
    return f"{product_code}{month}{shifted % 10}"


@st.cache_data(ttl="5m", show_spinner=False)
def load_curve(product_code: str, api_key: str, as_of: str, n_contracts: int) -> pd.DataFrame:
    """Massive's contract list can lag a day right at midnight UTC rollover — the
    server's local date ticks over before the feed has published that date's active
    set, and /contracts comes back empty. Retry against yesterday rather than show
    a false "no live contracts" warning for what is really just feed lag."""
    d = date.fromisoformat(as_of)
    curve = get_futures_curve(product_code, api_key, d, n_contracts=n_contracts)
    if curve.empty:
        curve = get_futures_curve(product_code, api_key, d - timedelta(days=1), n_contracts=n_contracts)
    return curve


@st.cache_data(ttl="6h", show_spinner="Loading settlement history…")
def load_histories(tickers: tuple[str, ...], api_key: str) -> dict[str, pd.Series]:
    return get_settlement_histories(list(tickers), api_key)


@st.cache_data(ttl="6h", show_spinner="Building continuous nearby series…")
def build_continuous_series(product_code: str, api_key: str, as_of: str, curve: pd.DataFrame) -> pd.Series:
    """Splice consecutive front-month contracts into one unadjusted 'nearby' series:
    on each date, the settle of whichever contract expires soonest. This is the
    standard nearby-futures construction — not back-adjusted, so a small gap can
    appear at each roll where the new front month settled at a different level."""
    as_of_date = date.fromisoformat(as_of)
    year_digit = as_of_date.year % 10
    tickers = []
    for letter in STANDARD_MONTHS.get(product_code, ""):
        baseline = f"{product_code}{letter}{year_digit}"
        for back in range(CONTINUOUS_YEARS_BACK):
            t = shift_ticker_year(baseline, product_code, -back)
            if t:
                tickers.append(t)

    hist = get_settlement_histories(sorted(set(tickers)), api_key)
    live_expiry = dict(zip(curve["ticker"], curve["expiration"]))

    legs = []
    for t, series in hist.items():
        if series is None or not len(series):
            continue
        expiry = live_expiry.get(t, series.index.max())
        legs.append((expiry, series))
    legs.sort(key=lambda pair: pair[0])

    continuous: dict = {}
    prev_expiry = None
    for expiry, series in legs:
        segment = series[series.index <= expiry]
        if prev_expiry is not None:
            segment = segment[segment.index > prev_expiry]
        continuous.update(segment.to_dict())
        prev_expiry = expiry

    if not continuous:
        return pd.Series(dtype=float)
    result = pd.Series(continuous).sort_index()
    result.index = pd.to_datetime(result.index)
    return result


def resample_ohlc(series: pd.Series, rule: str) -> pd.DataFrame:
    """Weekly/monthly bars derived from daily settlement closes — there's no true
    intraday high/low in this feed, so open/high/low/close are the first, max,
    min, and last daily close within each period."""
    ohlc = series.resample(rule).agg(["first", "max", "min", "last"]).dropna()
    ohlc.columns = ["open", "high", "low", "close"]
    return ohlc


def highs_lows_by_month(series: pd.Series) -> tuple[pd.DataFrame, pd.DataFrame, list[int]]:
    """Per-year date/price of the annual high and low, plus a month-frequency
    summary across complete calendar years (a year in progress, or a partial
    year at the start of the feed's history, is excluded from the summary
    since its high/low can only fall within the months already elapsed)."""
    df = series.rename("price").to_frame()
    df["year"] = df.index.year
    rows = []
    for yr, g in df.groupby("year"):
        high_date = g["price"].idxmax()
        low_date = g["price"].idxmin()
        complete = (
            g.index.min() <= pd.Timestamp(year=int(yr), month=1, day=10)
            and g.index.max() >= pd.Timestamp(year=int(yr), month=12, day=20)
        )
        rows.append({
            "Year": int(yr),
            "High date": high_date.date(),
            "High price": float(g.loc[high_date, "price"]),
            "High month": high_date.strftime("%b"),
            "Low date": low_date.date(),
            "Low price": float(g.loc[low_date, "price"]),
            "Low month": low_date.strftime("%b"),
            "Complete year": complete,
        })
    per_year = pd.DataFrame(rows)
    if per_year.empty:
        return per_year, pd.DataFrame(), []

    complete_years = per_year[per_year["Complete year"]]
    excluded = sorted(per_year.loc[~per_year["Complete year"], "Year"].tolist())
    n = len(complete_years)
    if n == 0:
        return per_year, pd.DataFrame(), excluded

    high_counts = complete_years["High month"].value_counts().reindex(MONTH_ORDER, fill_value=0)
    low_counts = complete_years["Low month"].value_counts().reindex(MONTH_ORDER, fill_value=0)
    summary = pd.DataFrame({
        "Month": MONTH_ORDER,
        "Years w/ high": high_counts.values,
        "% high": (high_counts.values / n * 100).round(0).astype(int),
        "Years w/ low": low_counts.values,
        "% low": (low_counts.values / n * 100).round(0).astype(int),
    })
    return per_year, summary, excluded


def plotly_config(filename: str) -> dict:
    return {
        "displayModeBar": True,
        "displaylogo": False,
        "toImageButtonOptions": {"format": "png", "filename": filename,
                                 "height": 700, "width": 1400, "scale": 2},
    }


@st.cache_data(show_spinner=False, max_entries=32)
def figure_png(fig_json: str) -> bytes | None:
    try:
        import plotly.io as pio
        return pio.from_json(fig_json).to_image(format="png", width=1400, height=700, scale=2)
    except Exception:
        return None


def export_row(frame: pd.DataFrame, filename: str, key: str, fig=None):
    row = st.container(horizontal=True, vertical_alignment="center")
    with row:
        tsv = frame.to_csv(sep="\t", index=False)
        with st.popover("Copy", width=90):
            st.caption("Tab-separated — use the copy icon, then paste into Excel.")
            st.code(tsv, language=None, height=260)
        st.download_button("CSV", frame.to_csv(index=False).encode(), f"{filename}.csv",
                           "text/csv", key=f"csv_{key}", width=90)
        if fig is not None:
            if st.button("PNG", key=f"png_btn_{key}", width=90,
                         help="Render this chart as a PNG for download."):
                st.session_state[f"png_ready_{key}"] = True
            if st.session_state.get(f"png_ready_{key}"):
                data = figure_png(fig.to_json())
                if data:
                    st.download_button("Save PNG", data, f"{filename}.png", "image/png",
                                       key=f"png_dl_{key}", width=120)
                else:
                    st.caption("PNG export unavailable — use the camera icon on the chart.")


def _add_vline(fig, x, text, color):
    x = pd.Timestamp(x) if isinstance(x, date) else x
    fig.add_shape(type="line", xref="x", yref="paper", x0=x, x1=x, y0=0, y1=1,
                  line=dict(color=color, dash="dash", width=1.5))
    fig.add_annotation(x=x, xref="x", y=1.0, yref="paper", text=text, showarrow=False,
                       yanchor="bottom", font=dict(size=10, color=color))


def _style_axes(fig, y_title, x_title, height=420):
    wm = watermark_path()
    if wm:
        fig.add_layout_image(dict(
            source=watermark_uri(wm), xref="paper", yref="paper",
            x=0.5, y=0.5, sizex=0.5, sizey=0.5,
            xanchor="center", yanchor="middle", sizing="contain",
            opacity=WATERMARK_OPACITY, layer="below",
        ))
    fig.update_layout(
        height=height, margin=dict(l=10, r=20, t=30, b=10),
        yaxis_title=y_title, xaxis_title=x_title,
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, font=dict(size=10)),
    )
    fig.update_yaxes(gridcolor="#eceff1", zeroline=True, zerolinecolor="#cfd8dc", automargin=True)
    fig.update_xaxes(gridcolor="#eceff1")


WINDOW_CHOICES = {"6M": 183, "1Y": 365, "18M": 548}


def _year_grid_average(by_dte: dict[str, pd.Series], window_days: int) -> pd.Series:
    """Put every year on a common daily grid, then average only where most years
    are present — avoids the mean lurching between 'all years' and 'one lonely year'
    at the edges of the window."""
    grid = pd.RangeIndex(-window_days, 1)
    aligned = {}
    for name, s in by_dte.items():
        clean = s[~s.index.duplicated(keep="last")].sort_index()
        aligned[name] = clean.reindex(grid).interpolate(limit_area="inside")
    frame = pd.DataFrame(aligned)
    required = max(2, (len(aligned) + 1) // 2)
    return frame.mean(axis=1, skipna=True)[frame.count(axis=1) >= required]


def render_seasonal_futures(commodity: dict, api_key: str, as_of: date):
    code = commodity["product_code"]
    key = commodity["key"]
    unit = commodity["unit"]

    try:
        curve = load_curve(code, api_key, as_of.isoformat(), 8)
    except MassiveApiError as e:
        st.error(f"Couldn't load {commodity['label']} quotes: {e}")
        return
    if curve.empty:
        st.warning(f"{commodity['label']}: no live contracts.")
        return

    tickers = list(curve["ticker"])
    expiries = dict(zip(curve["ticker"], curve["expiration"]))

    row = st.container(horizontal=True, vertical_alignment="bottom")
    with row:
        ticker = st.selectbox(
            "Contract", tickers, key=f"fut_ticker_{key}", width=150,
            format_func=lambda t: friendly_contract(t, code),
        )
        years_back = st.slider("Prior years", 1, MAX_YEARS_BACK, 4, key=f"fut_years_{key}", width=170)
        indexed = st.toggle("Indexed (start = 100)", value=True, key=f"fut_idx_{key}",
                            help="Rebase each year to 100 at the start of the window, so years with "
                                 "very different price levels can be compared on shape alone.")
        show_avg = st.toggle("Average", value=True, key=f"fut_avg_{key}")
        window_label = st.segmented_control("Window", list(WINDOW_CHOICES), default="1Y", key=f"fut_win_{key}")
        show_reports = st.toggle("Report dates", value=True, key=f"fut_rpt_{key}",
                                 help="USDA report release dates relevant to this market.")

    window_days = WINDOW_CHOICES.get(window_label or "1Y", 365)
    anchor_expiry = expiries[ticker]
    label = friendly_contract(ticker, code)
    y_title = "Index (start = 100)" if indexed else f"Price ({unit})"
    fmt = ".1f" if indexed else ".3f"
    report_dates = relevant_report_dates(code, as_of) if show_reports else {}

    shifted = [shift_ticker_year(ticker, code, -b) for b in range(years_back + 1)]
    shifted = [t for t in shifted if t]
    hist = load_histories(tuple(shifted), api_key)

    st.caption(f"**{label}** — recent history")
    current = hist.get(ticker)
    if current is None or not len(current):
        st.info("No settlement history for this contract yet.")
    else:
        cutoff = as_of - timedelta(days=window_days)
        shown = current[current.index >= cutoff]
        if not len(shown):
            st.info(f"No sessions inside the {window_label} window.")
        else:
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=list(shown.index), y=list(shown.values), mode="lines", name=label,
                line=dict(color=YEAR_COLORS[0], width=2),
                hovertemplate="%{x|%b %d, %Y}<br>%{y:.3f}<extra></extra>",
            ))
            if as_of <= anchor_expiry:
                _add_vline(fig, anchor_expiry, "expiration", EXP_COLOR)
                if code == "LE":
                    _add_vline(fig, live_cattle_fnd(anchor_expiry), "FND", FND_COLOR)
            if report_dates:
                add_report_vlines(fig, report_dates, shown.index.min(), shown.index.max())
            _style_axes(fig, f"Price ({unit})", None)
            fig.update_layout(showlegend=False)
            st.plotly_chart(fig, width="stretch", key=f"fut_hist_{key}",
                            config=plotly_config(f"{key}_{ticker}_history"))
            export_row(shown.rename("price").reset_index().rename(columns={"index": "date"}),
                       f"{key}_{ticker}_history", key=f"futhist_{key}")
            if report_dates:
                st.caption(report_legend_caption(code))

    st.caption(f"**{label}** — seasonal, aligned on expiration")
    fig = go.Figure()
    by_dte: dict[str, pd.Series] = {}
    skipped: list[str] = []

    current_year_range = None
    for back in range(years_back + 1):
        t = shift_ticker_year(ticker, code, -back)
        if not t:
            continue
        series = hist.get(t)
        if series is None or not len(series):
            skipped.append(t)
            continue
        year_expiry = expiries.get(t, series.index.max())
        dte = [-(year_expiry - d).days for d in series.index]
        keep = [i for i, d in enumerate(dte) if d >= -window_days]
        if not keep:
            skipped.append(t)
            continue

        ys = [series.values[i] for i in keep]
        if indexed:
            base = ys[0]
            if not base:
                skipped.append(t)
                continue
            ys = [y / base * 100 for y in ys]

        xs_dte = [dte[i] for i in keep]
        xs = [anchor_expiry + timedelta(days=d) for d in xs_dte]
        if back == 0:
            current_year_range = (min(xs), max(xs))
        name = t + (" (current)" if back == 0 else "")
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode="lines", name=name,
            line=dict(color=YEAR_COLORS[back % len(YEAR_COLORS)], width=3 if back == 0 else 1.5),
            opacity=1.0 if back == 0 else 0.75,
            hovertemplate=f"{name}<br>%{{y:{fmt}}}<extra></extra>",
        ))
        by_dte[t] = pd.Series(ys, index=pd.Index(xs_dte, name="dte"))

    if not by_dte:
        st.info("No settlement history available for this contract's prior-year analogs.")
    else:
        if show_avg and len(by_dte) > 1:
            avg = _year_grid_average(by_dte, window_days)
            if len(avg):
                fig.add_trace(go.Scatter(
                    x=[anchor_expiry + timedelta(days=int(d)) for d in avg.index],
                    y=list(avg.values), mode="lines", name=f"Avg ({len(by_dte)}yr)",
                    line=dict(color=AVG_COLOR, width=2.2, dash="dot"),
                    hovertemplate=f"Avg<br>%{{y:{fmt}}}<extra></extra>",
                ))
        if as_of <= anchor_expiry:
            _add_vline(fig, anchor_expiry, "expiration", EXP_COLOR)
            if code == "LE":
                _add_vline(fig, live_cattle_fnd(anchor_expiry), "FND", FND_COLOR)
        if report_dates and current_year_range:
            add_report_vlines(fig, report_dates, *current_year_range)
        _style_axes(fig, y_title, None)
        st.plotly_chart(fig, width="stretch", key=f"fut_seas_{key}",
                        config=plotly_config(f"{key}_{ticker}_seasonal"))
        export_row(pd.DataFrame(by_dte).sort_index().reset_index(),
                   f"{key}_{ticker}_seasonal", key=f"futseas_{key}")
        note = (
            f"{len(by_dte)} contract year{'s' if len(by_dte) != 1 else ''} overlaid · x = 0 is "
            f"{label}'s expiration, so every year lines up at the same point in its life."
        )
        if skipped:
            note += f" No usable history for {', '.join(skipped)}."
        st.caption(note)
        if report_dates:
            st.caption(report_legend_caption(code) + " (current year only.)")


def render_seasonal_spread(commodity: dict, api_key: str, as_of: date):
    code = commodity["product_code"]
    key = commodity["key"]
    unit = commodity["unit"]

    try:
        curve = load_curve(code, api_key, as_of.isoformat(), 10)
    except MassiveApiError as e:
        st.error(f"Couldn't load {commodity['label']} quotes: {e}")
        return
    if curve.empty or len(curve) < 2:
        st.warning(f"{commodity['label']}: not enough live contracts to build a spread.")
        return

    tickers = list(curve["ticker"])
    expiries = dict(zip(curve["ticker"], curve["expiration"]))

    row = st.container(horizontal=True, vertical_alignment="bottom")
    with row:
        near = st.selectbox("Near leg", tickers[:-1], key=f"sp_near_{key}", width=150,
                            format_func=lambda t: friendly_contract(t, code))
        later = [t for t in tickers if expiries[t] > expiries[near]]
        far = st.selectbox("Far leg", later, key=f"sp_far_{key}", width=150,
                           format_func=lambda t: friendly_contract(t, code))
        years_back = st.slider("Prior years", 1, MAX_YEARS_BACK, 4, key=f"sp_years_{key}", width=170)
        show_avg = st.toggle("Average", value=True, key=f"sp_avg_{key}")
        window_label = st.segmented_control("Window", list(WINDOW_CHOICES), default="1Y", key=f"sp_win_{key}")
        show_reports = st.toggle("Report dates", value=True, key=f"sp_rpt_{key}",
                                 help="USDA report release dates relevant to this market.")

    if not far:
        st.info("Pick a far leg that expires after the near leg.")
        return

    window_days = WINDOW_CHOICES.get(window_label or "1Y", 365)
    anchor_expiry = expiries[near]
    label = f"{friendly_contract(near, code)} / {friendly_contract(far, code)}"
    y_title = f"Spread ({unit})"
    report_dates = relevant_report_dates(code, as_of) if show_reports else {}

    shifted_pairs = []
    for back in range(years_back + 1):
        n = shift_ticker_year(near, code, -back)
        f = shift_ticker_year(far, code, -back)
        if n and f:
            shifted_pairs.append((n, f))
    tickers_needed = tuple(sorted({t for pair in shifted_pairs for t in pair}))
    hist = load_histories(tickers_needed, api_key)

    st.caption(f"**{label}** — recent history")
    near_h, far_h = hist.get(near), hist.get(far)
    if near_h is None or far_h is None or not len(near_h) or not len(far_h):
        st.info("No overlapping settlement history for this pair.")
    else:
        spread = (near_h - far_h).dropna()
        cutoff = as_of - timedelta(days=window_days)
        shown = spread[spread.index >= cutoff]
        if not len(shown):
            st.info(f"No sessions inside the {window_label} window.")
        else:
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=list(shown.index), y=list(shown.values), mode="lines", name=label,
                line=dict(color=YEAR_COLORS[0], width=2),
                hovertemplate="%{x|%b %d, %Y}<br>%{y:+.3f}<extra></extra>",
            ))
            if report_dates:
                add_report_vlines(fig, report_dates, shown.index.min(), shown.index.max())
            _style_axes(fig, y_title, None)
            fig.update_layout(showlegend=False)
            st.plotly_chart(fig, width="stretch", key=f"sp_hist_{key}",
                            config=plotly_config(f"{key}_{near}_{far}_history"))
            export_row(shown.rename("spread").reset_index().rename(columns={"index": "date"}),
                       f"{key}_{near}_{far}_history", key=f"sphist_{key}")
            if report_dates:
                st.caption(report_legend_caption(code))

    st.caption(f"**{label}** — seasonal, aligned on near-leg expiration")
    fig = go.Figure()
    by_dte: dict[str, pd.Series] = {}
    skipped: list[str] = []

    current_year_range = None
    for back, (n, f) in enumerate(shifted_pairs):
        n_h, f_h = hist.get(n), hist.get(f)
        if n_h is None or f_h is None or not len(n_h) or not len(f_h):
            skipped.append(f"{n}/{f}")
            continue
        spread = (n_h - f_h).dropna()
        if not len(spread):
            skipped.append(f"{n}/{f}")
            continue
        near_expiry = expiries.get(n, n_h.index.max())
        dte = [-(near_expiry - d).days for d in spread.index]
        keep = [i for i, d in enumerate(dte) if d >= -window_days]
        if not keep:
            skipped.append(f"{n}/{f}")
            continue

        xs_dte = [dte[i] for i in keep]
        xs = [anchor_expiry + timedelta(days=d) for d in xs_dte]
        if back == 0:
            current_year_range = (min(xs), max(xs))
        ys = [spread.values[i] for i in keep]
        name = f"{n}/{f}" + (" (current)" if back == 0 else "")
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode="lines", name=name,
            line=dict(color=YEAR_COLORS[back % len(YEAR_COLORS)], width=3 if back == 0 else 1.5),
            opacity=1.0 if back == 0 else 0.75,
            hovertemplate=f"{name}<br>%{{y:+.3f}}<extra></extra>",
        ))
        by_dte[f"{n}/{f}"] = pd.Series(ys, index=pd.Index(xs_dte, name="dte"))

    if not by_dte:
        st.info("No overlapping settlement history for this pair's prior-year analogs.")
    else:
        if show_avg and len(by_dte) > 1:
            avg = _year_grid_average(by_dte, window_days)
            if len(avg):
                fig.add_trace(go.Scatter(
                    x=[anchor_expiry + timedelta(days=int(d)) for d in avg.index],
                    y=list(avg.values), mode="lines", name=f"Avg ({len(by_dte)}yr)",
                    line=dict(color=AVG_COLOR, width=2.2, dash="dot"),
                    hovertemplate="Avg<br>%{y:+.3f}<extra></extra>",
                ))
        if as_of <= anchor_expiry:
            _add_vline(fig, anchor_expiry, "near expiration", EXP_COLOR)
            if code == "LE":
                _add_vline(fig, live_cattle_fnd(anchor_expiry), "near FND", FND_COLOR)
        if report_dates and current_year_range:
            add_report_vlines(fig, report_dates, *current_year_range)
        _style_axes(fig, y_title, None)
        st.plotly_chart(fig, width="stretch", key=f"sp_seas_{key}",
                        config=plotly_config(f"{key}_{near}_{far}_seasonal"))
        export_row(pd.DataFrame(by_dte).sort_index().reset_index(),
                   f"{key}_{near}_{far}_seasonal", key=f"spseas_{key}")
        note = (
            f"{len(by_dte)} contract year{'s' if len(by_dte) != 1 else ''} overlaid · x = 0 is "
            f"the near leg's expiration, so every year lines up at the same point in its life."
        )
        if skipped:
            note += f" No usable history for {', '.join(skipped)}."
        st.caption(note)
        if report_dates:
            st.caption(report_legend_caption(code) + " (current year only.)")


def build_spread_matrix(curve: pd.DataFrame, code: str) -> tuple[pd.DataFrame, list[str]]:
    """Near contracts down the rows, deferred contracts across the columns — every
    near/far combination's current nominal spread (near price - far price) at once."""
    legs = list(curve.itertuples(index=False))
    labels = [friendly_contract(r.ticker, code) for r in legs]
    rows = []
    for i, near in enumerate(legs):
        row = {"Contract": labels[i], "Price": near.price}
        for j, far in enumerate(legs):
            col = labels[j]
            row[col] = near.price - far.price if j > i else None
        rows.append(row)
    return pd.DataFrame(rows), labels


def render_spread_matrix(commodity: dict, api_key: str, as_of: date):
    code = commodity["product_code"]
    key = commodity["key"]
    unit = commodity["unit"]

    st.caption(
        f"Every near month against every deferred month at once, in {unit} "
        "(near price − far price). Positive = near trading over far (inverted); "
        "negative = near trading under far (normal carry-market shape)."
    )
    n_load = st.slider("Contract months", 3, 10, 8, key=f"mx_months_{key}", width=200)

    try:
        curve = load_curve(code, api_key, as_of.isoformat(), n_load)
    except MassiveApiError as e:
        st.error(f"Couldn't load {commodity['label']} quotes: {e}")
        return
    if curve.empty or len(curve) < 2:
        st.warning("Not enough live contracts to build a matrix.")
        return

    frame, labels = build_spread_matrix(curve, code)
    display = frame.copy()
    display["Price"] = [f"{v:.3f}" for v in frame["Price"]]
    for col in labels:
        display[col] = [f"{v:+.3f}" if v is not None and pd.notna(v) else "" for v in frame[col]]

    def zebra(row: pd.Series):
        return [GROUP_BAND if row.name % 2 == 0 else ""] * len(row)

    styler = display.style.apply(zebra, axis=1)
    with st.container(key=f"tablewrap_mx_{key}"):
        st.dataframe(styler, hide_index=True, width="stretch",
                     height=min(35 * (len(display) + 1) + 3, 500))
    export_row(display, f"spread_matrix_{key}", key=f"mx_{key}")


CONTINUOUS_FREQ = {"Daily": None, "Weekly": "W-FRI", "Monthly": "ME"}


def render_continuous_chart(commodity: dict, api_key: str, as_of: date):
    code = commodity["product_code"]
    key = commodity["key"]
    unit = commodity["unit"]

    try:
        curve = load_curve(code, api_key, as_of.isoformat(), 15)
    except MassiveApiError as e:
        st.error(f"Couldn't load {commodity['label']} quotes: {e}")
        return

    series = build_continuous_series(code, api_key, as_of.isoformat(), curve)
    if not len(series):
        st.warning(f"{commodity['label']}: no continuous history available.")
        return

    freq_label = st.segmented_control(
        "Bar interval", list(CONTINUOUS_FREQ), default="Daily", key=f"cont_freq_{key}",
    )
    rule = CONTINUOUS_FREQ.get(freq_label or "Daily")

    st.caption(
        f"Unadjusted nearby continuous {commodity['label']} — splices each front-month contract's "
        "settlements together at roll (its last trading day), so a small gap can appear where the new "
        "front month settled at a different level. Not back-adjusted."
    )

    fig = go.Figure()
    if rule is None:
        fig.add_trace(go.Scatter(
            x=list(series.index), y=list(series.values), mode="lines", name="Nearby",
            line=dict(color=YEAR_COLORS[0], width=1.5),
            hovertemplate="%{x|%b %d, %Y}<br>%{y:.3f}<extra></extra>",
        ))
    else:
        ohlc = resample_ohlc(series, rule)
        fig.add_trace(go.Candlestick(
            x=list(ohlc.index), open=ohlc["open"], high=ohlc["high"], low=ohlc["low"], close=ohlc["close"],
            increasing_line_color=UP_COLOR, decreasing_line_color=DOWN_COLOR, name="Nearby",
        ))
        fig.update_layout(xaxis_rangeslider_visible=False)
    _style_axes(fig, f"Price ({unit})", None, height=440)
    fig.update_layout(showlegend=False)
    st.plotly_chart(fig, width="stretch", key=f"cont_chart_{key}",
                    config=plotly_config(f"{key}_continuous_{freq_label}"))
    export_row(series.rename("price").reset_index().rename(columns={"index": "date"}),
               f"{key}_continuous_{freq_label}", key=f"cont_{key}")

    st.divider()
    st.caption(f"**{commodity['label']}** — historical highs & lows by calendar month (nearby continuous)")
    per_year, summary, excluded = highs_lows_by_month(series)
    if per_year.empty:
        st.info("Not enough continuous history yet to compute yearly highs and lows.")
        return

    display_year = per_year.copy()
    display_year["High price"] = display_year["High price"].map(lambda v: f"{v:.3f}")
    display_year["Low price"] = display_year["Low price"].map(lambda v: f"{v:.3f}")
    display_year = display_year[["Year", "High date", "High price", "Low date", "Low price"]]
    st.dataframe(display_year, hide_index=True, width="stretch")
    export_row(display_year, f"{key}_yearly_highlow", key=f"yrhl_{key}")

    if not summary.empty:
        note = f"Frequency across {len(per_year) - len(excluded)} complete calendar year"
        note += "s" if (len(per_year) - len(excluded)) != 1 else ""
        if excluded:
            note += f" (excludes partial year{'s' if len(excluded) != 1 else ''} {', '.join(map(str, excluded))})"
        st.caption(note + ".")
        st.dataframe(summary, hide_index=True, width="stretch")
        export_row(summary, f"{key}_month_frequency", key=f"mf_{key}")
    else:
        st.info("Not enough complete calendar years yet to compute a month-frequency summary.")

    st.divider()
    st.caption(f"**{commodity['label']}** — USDA report release calendar")
    st.caption(
        "Skipped as chart markers here — at monthly/quarterly cadence over this much history they'd "
        "read as a solid grid rather than a signal. Shown as a table instead, over the same span as "
        "the chart above."
    )
    reports = REPORT_RELEVANCE.get(code, [])
    since = series.index.min().date().isoformat()
    rows = []
    for report in reports:
        for d in fetch_report_dates(report, since):
            rows.append({"Date": d, "Report": report})
    if not rows:
        st.info("No report dates available for this range.")
    else:
        calendar = pd.DataFrame(rows).sort_values("Date", ascending=False).reset_index(drop=True)
        st.dataframe(calendar, hide_index=True, width="stretch", height=min(35 * (len(calendar) + 1) + 3, 400))
        export_row(calendar, f"{key}_report_calendar", key=f"rptcal_{key}")


def _init_builder_legs():
    if "sb_leg_count" not in st.session_state:
        st.session_state.sb_leg_count = 2


def render_spread_builder(api_key: str, as_of: date):
    st.caption(
        "Build a spread across 2 or more legs, any of them any commodity or contract month — "
        "a same-commodity calendar spread, a cross-commodity spread (e.g. Live Cattle vs. Feeder "
        "Cattle), or a weighted combination like a butterfly (+1 / −2 / +1)."
    )
    _init_builder_legs()
    codes = [c["product_code"] for c in COMMODITIES]

    # Default every leg to the same commodity (an ordinary calendar spread) —
    # cross-commodity is something the user opts into via the dropdown, not
    # something a fresh spread should start as.
    for i in range(st.session_state.sb_leg_count):
        st.session_state.setdefault(f"sb_code_{i}", codes[0])

    curves: dict[str, pd.DataFrame] = {}

    def get_curve(code: str) -> pd.DataFrame | None:
        if code not in curves:
            try:
                curves[code] = load_curve(code, api_key, as_of.isoformat(), 10)
            except MassiveApiError as e:
                st.error(f"Couldn't load {COMMODITY_BY_CODE[code]['label']} quotes: {e}")
                return None
            if curves[code].empty:
                st.warning(f"{COMMODITY_BY_CODE[code]['label']}: no live contracts.")
                return None
        return curves[code]

    legs = []
    for i in range(st.session_state.sb_leg_count):
        row = st.container(horizontal=True, vertical_alignment="bottom")
        with row:
            code = st.selectbox(
                "Commodity", codes, key=f"sb_code_{i}",
                format_func=lambda p: COMMODITY_BY_CODE[p]["label"], width=140,
            )
            curve = get_curve(code)
            if curve is None:
                return
            tickers = list(curve["ticker"])
            # Key includes `code` — a leg's contract options depend on its own
            # commodity choice, so switching commodity must be a fresh widget,
            # not the same key reinterpreting a stale ticker against new options.
            # index only seeds the value the first time this exact key exists,
            # so it's a harmless no-op once the user has picked their own contract;
            # it just makes a fresh leg default to the next month out, not the
            # same front month as every other leg.
            ticker = st.selectbox(
                "Contract", tickers, index=min(i, len(tickers) - 1),
                key=f"sb_ticker_{i}_{code}", width=130,
                format_func=lambda t, c=code: friendly_contract(t, c),
            )
            st.session_state.setdefault(f"sb_weight_{i}", 1 if i % 2 == 0 else -1)
            weight = st.number_input("Weight", min_value=-5, max_value=5, step=1,
                                     key=f"sb_weight_{i}", width=90)
            if st.session_state.sb_leg_count > 2:
                if st.button(":material/close:", key=f"sb_remove_{i}", help="Remove this leg"):
                    st.session_state.sb_leg_count -= 1
                    st.rerun()
        legs.append({"code": code, "ticker": ticker, "weight": weight,
                     "expiry": dict(zip(curve["ticker"], curve["expiration"]))[ticker]})

    if st.button(":material/add: Add leg", key="sb_add_leg"):
        st.session_state.sb_leg_count += 1
        st.rerun()

    codes_used = {leg["code"] for leg in legs}
    all_expiries: dict[str, date] = {}
    for code in codes_used:
        curve = curves.get(code)
        if curve is not None:
            all_expiries.update(dict(zip(curve["ticker"], curve["expiration"])))

    controls = st.container(horizontal=True, vertical_alignment="bottom")
    with controls:
        years_back = st.slider("Prior years", 1, MAX_YEARS_BACK, 4, key="sb_years", width=170)
        show_avg = st.toggle("Average", value=True, key="sb_avg")
        window_label = st.segmented_control("Window", list(WINDOW_CHOICES), default="1Y", key="sb_win")
        show_reports = st.toggle("Report dates", value=True, key="sb_rpt",
                                 help="USDA report release dates relevant to the commodities in this spread.")

    window_days = WINDOW_CHOICES.get(window_label or "1Y", 365)
    anchor = legs[0]
    anchor_expiry = anchor["expiry"]
    label = " / ".join(f"{friendly_contract(leg['ticker'], leg['code'])} ({leg['weight']:+d})" for leg in legs)
    units = {COMMODITY_BY_CODE[leg["code"]]["unit"] for leg in legs}
    y_title = f"Spread ({next(iter(units))})" if len(units) == 1 else "Spread (mixed units)"

    report_dates = {}
    if show_reports:
        for code in codes_used:
            for r in REPORT_RELEVANCE.get(code, []):
                report_dates.setdefault(r, [])
        report_dates = {r: fetch_report_dates(r, (as_of - timedelta(days=730)).isoformat()) for r in report_dates}

    all_tickers = set()
    shifted_legs_by_year = []
    for back in range(years_back + 1):
        shifted = []
        ok = True
        for leg in legs:
            t = shift_ticker_year(leg["ticker"], leg["code"], -back)
            if not t:
                ok = False
                break
            shifted.append({**leg, "ticker": t})
            all_tickers.add(t)
        shifted_legs_by_year.append(shifted if ok else None)

    hist = load_histories(tuple(sorted(all_tickers)), api_key)

    def combined_series(shifted_legs):
        series_list = []
        for leg in shifted_legs:
            s = hist.get(leg["ticker"])
            if s is None or not len(s):
                return None
            series_list.append(s * leg["weight"])
        combo = series_list[0]
        for s in series_list[1:]:
            combo = combo.add(s, fill_value=None)
        return combo.dropna()

    st.caption(f"**{label}** — recent history")
    current = combined_series(shifted_legs_by_year[0]) if shifted_legs_by_year[0] else None
    if current is None or not len(current):
        st.info("No overlapping settlement history for this combination of legs.")
    else:
        cutoff = as_of - timedelta(days=window_days)
        shown = current[current.index >= cutoff]
        if not len(shown):
            st.info(f"No sessions inside the {window_label} window.")
        else:
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=list(shown.index), y=list(shown.values), mode="lines", name=label,
                line=dict(color=YEAR_COLORS[0], width=2),
                hovertemplate="%{x|%b %d, %Y}<br>%{y:+.3f}<extra></extra>",
            ))
            if as_of <= anchor_expiry:
                _add_vline(fig, anchor_expiry, "leg 1 expiration", EXP_COLOR)
                if anchor["code"] == "LE":
                    _add_vline(fig, live_cattle_fnd(anchor_expiry), "leg 1 FND", FND_COLOR)
            if report_dates:
                add_report_vlines(fig, report_dates, shown.index.min(), shown.index.max())
            _style_axes(fig, y_title, None)
            fig.update_layout(showlegend=False)
            st.plotly_chart(fig, width="stretch", key="sb_hist",
                            config=plotly_config("spread_builder_history"))
            export_row(shown.rename("spread").reset_index().rename(columns={"index": "date"}),
                       "spread_builder_history", key="sb_hist_exp")
            if report_dates:
                st.caption("Dotted vertical lines mark USDA report release dates for the commodities "
                          "in this spread: " + ", ".join(
                              f"{r} ({REPORT_COLOR_NAMES[r]})" for r in report_dates) + ".")

    st.caption(f"**{label}** — seasonal, aligned on leg 1's expiration")
    fig = go.Figure()
    by_dte: dict[str, pd.Series] = {}
    skipped: list[str] = []
    current_year_range = None

    for back in range(years_back + 1):
        shifted = shifted_legs_by_year[back]
        combo_label = " / ".join(leg["ticker"] for leg in shifted) if shifted else f"back-{back}"
        if not shifted:
            skipped.append(combo_label)
            continue
        combo = combined_series(shifted)
        if combo is None or not len(combo):
            skipped.append(combo_label)
            continue
        leg1_ticker = shifted[0]["ticker"]
        leg1_series = hist.get(leg1_ticker)
        year_expiry = all_expiries.get(leg1_ticker, leg1_series.index.max() if leg1_series is not None and len(leg1_series) else None)
        if year_expiry is None:
            skipped.append(combo_label)
            continue
        dte = [-(year_expiry - d).days for d in combo.index]
        keep = [i for i, d in enumerate(dte) if d >= -window_days]
        if not keep:
            skipped.append(combo_label)
            continue

        xs_dte = [dte[i] for i in keep]
        xs = [anchor_expiry + timedelta(days=d) for d in xs_dte]
        if back == 0:
            current_year_range = (min(xs), max(xs))
        ys = [combo.values[i] for i in keep]
        name = combo_label + (" (current)" if back == 0 else "")
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode="lines", name=name,
            line=dict(color=YEAR_COLORS[back % len(YEAR_COLORS)], width=3 if back == 0 else 1.5),
            opacity=1.0 if back == 0 else 0.75,
            hovertemplate=f"{name}<br>%{{y:+.3f}}<extra></extra>",
        ))
        by_dte[combo_label] = pd.Series(ys, index=pd.Index(xs_dte, name="dte"))

    if not by_dte:
        st.info("No overlapping settlement history for this combination's prior-year analogs.")
    else:
        if show_avg and len(by_dte) > 1:
            avg = _year_grid_average(by_dte, window_days)
            if len(avg):
                fig.add_trace(go.Scatter(
                    x=[anchor_expiry + timedelta(days=int(d)) for d in avg.index],
                    y=list(avg.values), mode="lines", name=f"Avg ({len(by_dte)}yr)",
                    line=dict(color=AVG_COLOR, width=2.2, dash="dot"),
                    hovertemplate="Avg<br>%{y:+.3f}<extra></extra>",
                ))
        if as_of <= anchor_expiry:
            _add_vline(fig, anchor_expiry, "leg 1 expiration", EXP_COLOR)
            if anchor["code"] == "LE":
                _add_vline(fig, live_cattle_fnd(anchor_expiry), "leg 1 FND", FND_COLOR)
        if report_dates and current_year_range:
            add_report_vlines(fig, report_dates, *current_year_range)
        _style_axes(fig, y_title, None)
        st.plotly_chart(fig, width="stretch", key="sb_seas",
                        config=plotly_config("spread_builder_seasonal"))
        export_row(pd.DataFrame(by_dte).sort_index().reset_index(),
                   "spread_builder_seasonal", key="sb_seas_exp")
        note = (
            f"{len(by_dte)} year{'s' if len(by_dte) != 1 else ''} overlaid · x = 0 is "
            f"leg 1's expiration, so every year lines up at the same point in its life."
        )
        if skipped:
            note += f" No usable history for {', '.join(skipped)}."
        st.caption(note)


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
CHAT_MAX_TOKENS = 1024
CHAT_MAX_TOOL_ROUNDS = 5
CHAT_MAX_MESSAGES = 40  # user+assistant combined, a cost backstop behind the passphrase gate

CHAT_SYSTEM_PROMPT = (
    "You are a data assistant for JSA's Livestock Seasonal Futures & Spreads dashboard, "
    "covering CME Live Cattle, Feeder Cattle, and Lean Hogs futures. Answer questions about "
    "pricing history and this dashboard's data using the tools provided — never state a "
    "specific price, date, or statistic without having just looked it up via a tool call in "
    "this turn. If a tool returns an error, say so plainly rather than guessing or falling "
    "back on general knowledge. Quotes are delayed per the Massive API; report dates come "
    "from USDA's ESMIS release calendar. Keep answers concise and reference the actual numbers "
    "you pulled. You are not a licensed financial advisor — report what the data shows, don't "
    "give trading or investment advice."
)

CHAT_TOOLS = [
    {
        "name": "get_price_series",
        "description": (
            "Recent continuous nearby futures price for a livestock commodity: latest price, "
            "price at the start of the window, change, and the high/low over that window."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "commodity": {"type": "string", "description": "Live Cattle, Feeder Cattle, or Lean Hogs (or LE/GF/HE)."},
                "days_back": {"type": "integer", "description": "Lookback window in days (default 90, max 1800)."},
            },
            "required": ["commodity"],
        },
    },
    {
        "name": "get_seasonal_stats",
        "description": (
            "Per calendar year, when the annual high and low settled for a livestock "
            "commodity's continuous nearby series, plus a month-frequency summary across "
            "complete calendar years."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"commodity": {"type": "string", "description": "Live Cattle, Feeder Cattle, or Lean Hogs."}},
            "required": ["commodity"],
        },
    },
    {
        "name": "get_report_dates",
        "description": (
            "Most recent USDA report release dates relevant to a livestock commodity (Cattle "
            "on Feed, Hogs and Pigs, Livestock Slaughter, WASDE), from USDA's official ESMIS "
            "release calendar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "commodity": {"type": "string", "description": "Live Cattle, Feeder Cattle, or Lean Hogs."},
                "count": {"type": "integer", "description": "How many recent dates per report type (default 8)."},
            },
            "required": ["commodity"],
        },
    },
    {
        "name": "get_current_curve",
        "description": (
            "Live CME futures curve for a livestock commodity: every currently-listed "
            "contract month, its price, and expiration date."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"commodity": {"type": "string", "description": "Live Cattle, Feeder Cattle, or Lean Hogs."}},
            "required": ["commodity"],
        },
    },
    {
        "name": "get_calendar_spread",
        "description": (
            "Current value and recent history of a calendar spread (near contract month "
            "minus far contract month) within one livestock commodity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "commodity": {"type": "string", "description": "Live Cattle, Feeder Cattle, or Lean Hogs."},
                "near_month": {"type": "string", "description": "Near leg's month, e.g. 'Aug' or 'August'."},
                "far_month": {"type": "string", "description": "Far leg's month, e.g. 'Oct' or 'October'."},
                "days_back": {"type": "integer", "description": "History window in days (default 180)."},
            },
            "required": ["commodity", "near_month", "far_month"],
        },
    },
]


def get_anthropic_key() -> str:
    try:
        key = st.secrets.get("ANTHROPIC_API_KEY", "")
    except Exception:
        key = ""
    return key or os.environ.get("ANTHROPIC_API_KEY", "")


def get_chat_passphrase() -> str:
    try:
        phrase = st.secrets.get("CHAT_PASSPHRASE", "")
    except Exception:
        phrase = ""
    return phrase or os.environ.get("CHAT_PASSPHRASE", "")


def _normalize_commodity(text: str) -> str | None:
    t = (text or "").strip().upper()
    if t in COMMODITY_BY_CODE:
        return t
    low = (text or "").strip().lower()
    if "feeder" in low:
        return "GF"
    if "hog" in low or "pork" in low:
        return "HE"
    if "live" in low or "cattle" in low or "beef" in low:
        return "LE"
    return None


def tool_get_price_series(massive_key: str, as_of: date, commodity: str, days_back: int = 90) -> dict:
    code = _normalize_commodity(commodity)
    if not code:
        return {"error": f"Unknown commodity '{commodity}'. Use Live Cattle, Feeder Cattle, or Lean Hogs."}
    try:
        curve = load_curve(code, massive_key, as_of.isoformat(), 15)
        series = build_continuous_series(code, massive_key, as_of.isoformat(), curve)
    except MassiveApiError as e:
        return {"error": str(e)}
    if not len(series):
        return {"error": "No continuous price history available."}
    days_back = max(5, min(int(days_back or 90), 1800))
    cutoff = pd.Timestamp(as_of) - pd.Timedelta(days=days_back)
    window = series[series.index >= cutoff]
    if not len(window):
        window = series
    latest_price = float(window.iloc[-1])
    first_price = float(window.iloc[0])
    return {
        "commodity": COMMODITY_BY_CODE[code]["label"],
        "unit": COMMODITY_BY_CODE[code]["unit"],
        "as_of": str(window.index.max().date()),
        "latest_price": round(latest_price, 3),
        "window_start": {"date": str(window.index.min().date()), "price": round(first_price, 3)},
        "change": round(latest_price - first_price, 3),
        "pct_change": round((latest_price / first_price - 1) * 100, 2) if first_price else None,
        "window_high": round(float(window.max()), 3),
        "window_high_date": str(window.idxmax().date()),
        "window_low": round(float(window.min()), 3),
        "window_low_date": str(window.idxmin().date()),
        "sessions": int(len(window)),
        "note": "Unadjusted nearby continuous series (front-month splice); quotes delayed per Massive API.",
    }


def tool_get_seasonal_stats(massive_key: str, as_of: date, commodity: str) -> dict:
    code = _normalize_commodity(commodity)
    if not code:
        return {"error": f"Unknown commodity '{commodity}'."}
    try:
        curve = load_curve(code, massive_key, as_of.isoformat(), 15)
        series = build_continuous_series(code, massive_key, as_of.isoformat(), curve)
    except MassiveApiError as e:
        return {"error": str(e)}
    per_year, summary, excluded = highs_lows_by_month(series)
    if per_year.empty:
        return {"error": "Not enough continuous history yet."}
    per_year_out = per_year[["Year", "High date", "High price", "Low date", "Low price", "Complete year"]].copy()
    per_year_out["High date"] = per_year_out["High date"].astype(str)
    per_year_out["Low date"] = per_year_out["Low date"].astype(str)
    result = {
        "commodity": COMMODITY_BY_CODE[code]["label"],
        "unit": COMMODITY_BY_CODE[code]["unit"],
        "per_year": per_year_out.to_dict("records"),
    }
    if not summary.empty:
        result["month_frequency"] = summary.to_dict("records")
        result["complete_years_counted"] = len(per_year) - len(excluded)
        result["excluded_partial_years"] = excluded
    return result


def tool_get_report_dates(commodity: str, as_of: date, count: int = 8) -> dict:
    code = _normalize_commodity(commodity)
    if not code:
        return {"error": f"Unknown commodity '{commodity}'."}
    reports = REPORT_RELEVANCE.get(code, [])
    since = (as_of - timedelta(days=730)).isoformat()
    count = max(1, min(int(count or 8), 20))
    out = {}
    for r in reports:
        dates = fetch_report_dates(r, since)
        out[r] = [str(d) for d in dates[-count:]]
    return {
        "commodity": COMMODITY_BY_CODE[code]["label"],
        "relevant_reports": out,
        "note": "Dates from USDA's ESMIS release calendar; most recent listed last.",
    }


def tool_get_current_curve(massive_key: str, as_of: date, commodity: str) -> dict:
    code = _normalize_commodity(commodity)
    if not code:
        return {"error": f"Unknown commodity '{commodity}'."}
    try:
        curve = load_curve(code, massive_key, as_of.isoformat(), 12)
    except MassiveApiError as e:
        return {"error": str(e)}
    if curve.empty:
        return {"error": "No live contracts."}
    rows = [
        {
            "contract": friendly_contract(r["ticker"], code),
            "ticker": r["ticker"],
            "price": round(float(r["price"]), 3),
            "expiration": str(r["expiration"]),
        }
        for _, r in curve.iterrows()
    ]
    return {"commodity": COMMODITY_BY_CODE[code]["label"], "unit": COMMODITY_BY_CODE[code]["unit"], "curve": rows}


def tool_get_calendar_spread(massive_key: str, as_of: date, commodity: str,
                             near_month: str, far_month: str, days_back: int = 180) -> dict:
    code = _normalize_commodity(commodity)
    if not code:
        return {"error": f"Unknown commodity '{commodity}'."}
    try:
        curve = load_curve(code, massive_key, as_of.isoformat(), 15)
    except MassiveApiError as e:
        return {"error": str(e)}
    if curve.empty:
        return {"error": "No live contracts."}
    tickers = list(curve["ticker"])

    def find_ticker(month_label: str) -> str | None:
        needle = (month_label or "").strip().lower()[:3]
        for t in tickers:
            if friendly_contract(t, code).lower().startswith(needle):
                return t
        return None

    near_t, far_t = find_ticker(near_month), find_ticker(far_month)
    if not near_t or not far_t:
        return {
            "error": "Couldn't match both contract months among currently listed contracts.",
            "currently_listed": [friendly_contract(t, code) for t in tickers],
        }
    hist = load_histories((near_t, far_t), massive_key)
    near_h, far_h = hist.get(near_t), hist.get(far_t)
    if near_h is None or far_h is None or not len(near_h) or not len(far_h):
        return {"error": "No overlapping settlement history for this pair."}
    spread = (near_h - far_h).dropna()
    days_back = max(5, min(int(days_back or 180), 1800))
    # near_h/far_h come straight from load_histories, indexed by plain date
    # objects (unlike build_continuous_series's DatetimeIndex) — a pd.Timestamp
    # cutoff here raises TypeError comparing Timestamp to date.
    cutoff = as_of - timedelta(days=days_back)
    window = spread[spread.index >= cutoff]
    if not len(window):
        window = spread
    return {
        "commodity": COMMODITY_BY_CODE[code]["label"],
        "unit": COMMODITY_BY_CODE[code]["unit"],
        "near_leg": friendly_contract(near_t, code),
        "far_leg": friendly_contract(far_t, code),
        "current_spread": round(float(window.iloc[-1]), 3),
        # window's index is already a plain date (from load_histories), not a
        # Timestamp — no .date() call needed, unlike the continuous-series tools.
        "as_of": str(window.index.max()),
        "window_high": round(float(window.max()), 3),
        "window_high_date": str(window.idxmax()),
        "window_low": round(float(window.min()), 3),
        "window_low_date": str(window.idxmin()),
        "sessions": int(len(window)),
    }


def execute_chat_tool(name: str, tool_input: dict, massive_key: str, as_of: date) -> dict:
    try:
        if name == "get_price_series":
            return tool_get_price_series(massive_key, as_of, tool_input.get("commodity", ""),
                                         tool_input.get("days_back", 90))
        if name == "get_seasonal_stats":
            return tool_get_seasonal_stats(massive_key, as_of, tool_input.get("commodity", ""))
        if name == "get_report_dates":
            return tool_get_report_dates(tool_input.get("commodity", ""), as_of, tool_input.get("count", 8))
        if name == "get_current_curve":
            return tool_get_current_curve(massive_key, as_of, tool_input.get("commodity", ""))
        if name == "get_calendar_spread":
            return tool_get_calendar_spread(massive_key, as_of, tool_input.get("commodity", ""),
                                            tool_input.get("near_month", ""), tool_input.get("far_month", ""),
                                            tool_input.get("days_back", 180))
        return {"error": f"Unknown tool '{name}'."}
    except MassiveApiError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Tool failed: {e}"}


def call_anthropic(anthropic_key: str, messages: list) -> dict:
    resp = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": anthropic_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": CHAT_MAX_TOKENS,
            "system": CHAT_SYSTEM_PROMPT,
            "messages": messages,
            "tools": CHAT_TOOLS,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def run_chat_turn(anthropic_key: str, massive_key: str, as_of: date, history: list) -> str:
    messages = list(history)
    for _ in range(CHAT_MAX_TOOL_ROUNDS):
        data = call_anthropic(anthropic_key, messages)
        content = data.get("content", [])
        if data.get("stop_reason") != "tool_use":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
            return text or "(no response)"
        messages.append({"role": "assistant", "content": content})
        tool_results = []
        for block in content:
            if block.get("type") != "tool_use":
                continue
            result = execute_chat_tool(block["name"], block.get("input", {}) or {}, massive_key, as_of)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": json.dumps(result, default=str),
            })
        messages.append({"role": "user", "content": tool_results})
    return "I couldn't finish looking that up within the allowed number of steps — try a narrower question."


def render_chat(massive_key: str, as_of: date):
    st.caption(
        "Ask about CME livestock pricing history and this dashboard's data — recent prices, "
        "seasonal highs/lows, USDA report dates, current curves, calendar spreads. Answers are "
        "grounded in the same data as the charts above via live tool calls, not general knowledge."
    )

    anthropic_key = get_anthropic_key()
    if not anthropic_key:
        st.error("No ANTHROPIC_API_KEY configured for this app.")
        return

    passphrase = get_chat_passphrase()
    if passphrase and not st.session_state.get("chat_unlocked"):
        entered = st.text_input("Passphrase", type="password", key="chat_pw")
        if st.button("Unlock", key="chat_unlock_btn"):
            if entered == passphrase:
                st.session_state.chat_unlocked = True
                st.rerun()
            else:
                st.error("Wrong passphrase.")
        return

    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []

    for msg in st.session_state.chat_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if len(st.session_state.chat_messages) >= CHAT_MAX_MESSAGES:
        st.info("This chat session has reached its message limit. Reload the page to start a new one.")
        return

    prompt = st.chat_input("Ask about livestock futures pricing or dashboard data…")
    if prompt:
        st.session_state.chat_messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        api_history = [{"role": m["role"], "content": m["content"]} for m in st.session_state.chat_messages]
        with st.chat_message("assistant"):
            with st.spinner("Looking that up…"):
                try:
                    reply = run_chat_turn(anthropic_key, massive_key, as_of, api_history)
                except requests.RequestException as e:
                    reply = f"Sorry, the chat request failed: {e}"
            st.markdown(reply)
        st.session_state.chat_messages.append({"role": "assistant", "content": reply})


def render_commodity(commodity: dict, api_key: str, as_of: date):
    tab_futures, tab_spread, tab_matrix, tab_continuous = st.tabs(
        ["Seasonal futures", "Seasonal spread", "Spread matrix", "Continuous chart"]
    )
    with tab_futures:
        render_seasonal_futures(commodity, api_key, as_of)
    with tab_spread:
        render_seasonal_spread(commodity, api_key, as_of)
    with tab_continuous:
        render_continuous_chart(commodity, api_key, as_of)
    with tab_matrix:
        render_spread_matrix(commodity, api_key, as_of)


def main():
    col_logo, col_title = st.columns([1, 6], vertical_alignment="center")
    with col_logo:
        st.image(asset(LOGO_FILE), width=150)
    with col_title:
        st.title("Livestock Seasonal Futures & Spreads")
    st.caption(
        "Live CME livestock futures, aligned across prior contract years to reveal seasonal "
        "patterns in outright price and in calendar spreads. "
        f"Data as of {datetime.now():%b %d, %Y %I:%M %p} · quotes delayed per Massive API."
    )
    st.caption(DATA_START_NOTE)

    api_key = get_api_key()
    if not api_key:
        st.error(
            "No MASSIVE_API_KEY found. Add it to `.streamlit/secrets.toml` "
            '(`MASSIVE_API_KEY = "..."`) or as an environment variable.'
        )
        st.stop()

    as_of = date.today()

    labels = [c["label"] for c in COMMODITIES] + ["Spread Builder", "Chat"]
    tabs = st.tabs(labels)
    for tab, commodity in zip(tabs, COMMODITIES):
        with tab:
            st.caption(commodity["sublabel"])
            if commodity["product_code"] == "LE":
                st.caption(FND_NOTE)
            render_commodity(commodity, api_key, as_of)
    with tabs[-2]:
        render_spread_builder(api_key, as_of)
    with tabs[-1]:
        render_chat(api_key, as_of)


if __name__ == "__main__":
    main()
