import base64
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
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

COMMODITIES = [
    {"key": "live_cattle", "label": "Live Cattle", "sublabel": "CME · LE", "product_code": "LE", "unit": "¢/lb"},
    {"key": "feeder_cattle", "label": "Feeder Cattle", "sublabel": "CME · GF", "product_code": "GF", "unit": "¢/lb"},
    {"key": "lean_hogs", "label": "Lean Hogs", "sublabel": "CME · HE", "product_code": "HE", "unit": "¢/lb"},
]

MONTH_LETTERS = {
    "F": "Jan", "G": "Feb", "H": "Mar", "J": "Apr", "K": "May", "M": "Jun",
    "N": "Jul", "Q": "Aug", "U": "Sep", "V": "Oct", "X": "Nov", "Z": "Dec",
}

YEAR_COLORS = ["#0693e3", "#e8833a", "#5aa469", "#b05fb0", "#9aa5b1", "#c0392b"]
AVG_COLOR = "#111111"
EXP_COLOR = "#c62828"
FND_COLOR = "#8e24aa"
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

    window_days = WINDOW_CHOICES.get(window_label or "1Y", 365)
    anchor_expiry = expiries[ticker]
    label = friendly_contract(ticker, code)
    y_title = "Index (start = 100)" if indexed else f"Price ({unit})"
    fmt = ".1f" if indexed else ".3f"

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
            _style_axes(fig, f"Price ({unit})", None)
            fig.update_layout(showlegend=False)
            st.plotly_chart(fig, width="stretch", key=f"fut_hist_{key}",
                            config=plotly_config(f"{key}_{ticker}_history"))
            export_row(shown.rename("price").reset_index().rename(columns={"index": "date"}),
                       f"{key}_{ticker}_history", key=f"futhist_{key}")

    st.caption(f"**{label}** — seasonal, aligned on expiration")
    fig = go.Figure()
    by_dte: dict[str, pd.Series] = {}
    skipped: list[str] = []

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

    if not far:
        st.info("Pick a far leg that expires after the near leg.")
        return

    window_days = WINDOW_CHOICES.get(window_label or "1Y", 365)
    anchor_expiry = expiries[near]
    label = f"{friendly_contract(near, code)} / {friendly_contract(far, code)}"
    y_title = f"Spread ({unit})"

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
            _style_axes(fig, y_title, None)
            fig.update_layout(showlegend=False)
            st.plotly_chart(fig, width="stretch", key=f"sp_hist_{key}",
                            config=plotly_config(f"{key}_{near}_{far}_history"))
            export_row(shown.rename("spread").reset_index().rename(columns={"index": "date"}),
                       f"{key}_{near}_{far}_history", key=f"sphist_{key}")

    st.caption(f"**{label}** — seasonal, aligned on near-leg expiration")
    fig = go.Figure()
    by_dte: dict[str, pd.Series] = {}
    skipped: list[str] = []

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


def render_commodity(commodity: dict, api_key: str, as_of: date):
    tab_futures, tab_spread, tab_matrix = st.tabs(["Seasonal futures", "Seasonal spread", "Spread matrix"])
    with tab_futures:
        render_seasonal_futures(commodity, api_key, as_of)
    with tab_spread:
        render_seasonal_spread(commodity, api_key, as_of)
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

    tabs = st.tabs([c["label"] for c in COMMODITIES])
    for tab, commodity in zip(tabs, COMMODITIES):
        with tab:
            st.caption(commodity["sublabel"])
            if commodity["product_code"] == "LE":
                st.caption(FND_NOTE)
            render_commodity(commodity, api_key, as_of)


if __name__ == "__main__":
    main()
