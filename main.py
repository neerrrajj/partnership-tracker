import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import requests
from datetime import datetime, date
import calendar
import json

st.set_page_config(page_title="Partnership Tracker", page_icon="📊", layout="wide")

PARTNERS = ["Ravi", "Neeraj", "Dharmesh"]

# Business actually started operating on this date. This predates any capital
# event and is a fixed historical fact, so it lives here rather than in a sheet.
PHASE1_START = date(2025, 11, 1)

# ─── Auth ───────────────────────────────────────────────────────────────────

@st.cache_resource
def get_gsheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(st.secrets["gcp_service_account"], scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(st.secrets["google"]["sheet_id"])

# ─── Capital Events — single sheet, single source of truth ─────────────────
# Columns: Date | Partner | Amount | Notes
# e.g.  3/11/25  | Ravi     | 855400 |
#       25/4/26  | Neeraj   | 50000  |
#       25/4/26  | Dharmesh | 50000  |
# Use a negative Amount for a withdrawal. No "Type" column needed.

@st.cache_data(ttl=300)
def get_capital_events():
    sh = get_gsheet()
    ws = sh.worksheet("Capital Events")
    df = pd.DataFrame(ws.get_all_records())
    if df.empty:
        return pd.DataFrame(columns=["Date", "Partner", "Amount", "Notes"])
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True)
    df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce").fillna(0)
    df = df.sort_values("Date").reset_index(drop=True)
    return df

def get_phase2_start(events_df: pd.DataFrame) -> date:
    """Phase 2 begins the day a partner OTHER than Ravi first contributes."""
    others = events_df[events_df["Partner"] != "Ravi"]
    if others.empty:
        # No one else has joined yet — treat "today" as a placeholder so the
        # app still runs; Phase 2 tables will simply be empty.
        return date.today()
    return others["Date"].min().date()

# ─── ERPNext P&L ────────────────────────────────────────────────────────────

def _run_pnl_report(from_date: str, to_date: str, periodicity: str):
    url = st.secrets["erpnext"]["url"].rstrip("/")
    api_key = st.secrets["erpnext"]["api_key"]
    api_secret = st.secrets["erpnext"]["api_secret"]
    company = st.secrets["erpnext"]["company"]

    headers = {"Authorization": f"token {api_key}:{api_secret}"}
    filters = {
        "company": company,
        "filter_based_on": "Date Range",
        "period_start_date": from_date,
        "period_end_date": to_date,
        "periodicity": periodicity,
        "accumulated_values": 0,
        "include_default_book_entries": 1,
    }
    resp = requests.post(
        f"{url}/api/method/frappe.desk.query_report.run",
        headers=headers,
        data={
            "report_name": "Profit and Loss Statement",
            "filters": json.dumps(filters),
            "ignore_prepared_report": "1",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("message", {})

def _extract_net_profit_row(data: dict):
    rows = data.get("result", [])
    cols = data.get("columns", [])
    account_idx = None
    for idx, col in enumerate(cols):
        if isinstance(col, dict) and col.get("fieldname", "").lower() == "account":
            account_idx = idx
            break

    for row in rows:
        if isinstance(row, dict):
            label = str(row.get("account", "") or row.get("Account", "")).lower()
        elif isinstance(row, list) and account_idx is not None:
            label = str(row[account_idx]).lower()
        else:
            continue

        if "net profit" in label or "profit for the year" in label or "net loss" in label:
            period_vals = {}
            for col in cols:
                if not isinstance(col, dict):
                    continue
                fieldname = col.get("fieldname", "")
                # Only real period/total currency columns — skip metadata
                # fields (acc_name, acc_number, currency, ...) which are
                # absent on this row and would otherwise default to 0.
                if not fieldname or fieldname == "account" or col.get("fieldtype") != "Currency":
                    continue
                if isinstance(row, dict):
                    if fieldname in row:
                        period_vals[fieldname] = row.get(fieldname, 0) or 0
                elif isinstance(row, list):
                    for idx, c in enumerate(cols):
                        if isinstance(c, dict) and c.get("fieldname") == fieldname:
                            period_vals[fieldname] = row[idx] if idx < len(row) else 0
                            break
            return period_vals
    return {}

@st.cache_data(ttl=300)
def get_pnl_for_range(from_date: date, to_date: date) -> float:
    """Total net profit for a single date range (Phase 1's one-line figure)."""
    if from_date > to_date:
        return 0.0
    data = _run_pnl_report(str(from_date), str(to_date), periodicity="Yearly")
    st.session_state["erpnext_raw_phase1"] = data
    period_vals = _extract_net_profit_row(data)
    for fieldname, val in period_vals.items():
        if fieldname != "total":
            return float(val or 0)
    return float(period_vals.get("total", 0) or 0)

@st.cache_data(ttl=300)
def get_monthly_pnl_map(from_date: date, to_date: date) -> dict:
    if from_date > to_date:
        return {}
    data = _run_pnl_report(str(from_date), str(to_date), periodicity="Monthly")
    st.session_state["erpnext_raw_phase2"] = data
    return _extract_net_profit_row(data)

def _lookup_month_profit(pnl_map: dict, month_start: date) -> float:
    for period_key, profit in pnl_map.items():
        if period_key == "total":
            continue
        try:
            period_date = pd.to_datetime(period_key)
        except Exception:
            try:
                period_date = pd.to_datetime(period_key, format="%b_%Y")
            except Exception:
                continue
        if period_date.year == month_start.year and period_date.month == month_start.month:
            return float(profit or 0)
    return 0.0

# ─── Capital account as of a given date, built purely from events ──────────

def capital_as_of(events_df: pd.DataFrame, partner: str, as_of: date) -> float:
    if events_df.empty:
        return 0.0
    events = events_df[(events_df["Partner"] == partner) & (events_df["Date"].dt.date <= as_of)]
    return float(events["Amount"].sum())

# ─── Phase 2 monthly allocation ─────────────────────────────────────────────

def compute_phase2(events_df: pd.DataFrame, phase2_start: date, opening_capital: dict, pnl_map: dict):
    capital = dict(opening_capital)
    today = date.today()
    results = []

    current_month_start = phase2_start
    while current_month_start <= today:
        last_day = calendar.monthrange(current_month_start.year, current_month_start.month)[1]
        month_end_full = date(current_month_start.year, current_month_start.month, last_day)
        month_end = min(month_end_full, today)

        month_label = current_month_start.strftime("%b %Y")
        if current_month_start.day > 1:
            month_label = f"{month_label} ({current_month_start.strftime('%d')}\u2013{month_end.strftime('%d')})"
        elif month_end < month_end_full:
            days_elapsed = (month_end - current_month_start).days + 1
            month_label = f"{month_label} ({days_elapsed}/{last_day})"

        net_profit = _lookup_month_profit(pnl_map, current_month_start)

        total_capital = sum(capital.values())
        ratios = {p: (capital[p] / total_capital if total_capital else 0) for p in PARTNERS}
        shares = {p: net_profit * ratios[p] for p in PARTNERS}

        for p in PARTNERS:
            capital[p] += shares[p]
            if not events_df.empty:
                if current_month_start == phase2_start:
                    # avoid re-counting the contribution that DEFINED phase2_start
                    mask = (events_df["Partner"] == p) & \
                           (events_df["Date"].dt.date > current_month_start) & \
                           (events_df["Date"].dt.date <= month_end)
                else:
                    mask = (events_df["Partner"] == p) & \
                           (events_df["Date"].dt.date >= current_month_start) & \
                           (events_df["Date"].dt.date <= month_end)
                month_events = events_df[mask]["Amount"].sum()
            else:
                month_events = 0
            capital[p] += month_events

        results.append({
            "Month": month_label,
            "Net Profit": net_profit,
            **{f"{p} Ratio": ratios[p] for p in PARTNERS},
            **{f"{p} Share": shares[p] for p in PARTNERS},
            **{f"{p} Capital A/C": capital[p] for p in PARTNERS},
        })

        if current_month_start.month == 12:
            current_month_start = date(current_month_start.year + 1, 1, 1)
        else:
            current_month_start = date(current_month_start.year, current_month_start.month + 1, 1)

    return results, capital

# ─── Formatting ─────────────────────────────────────────────────────────────

def inr(val):
    if val is None:
        return "—"
    sign = "-" if val < 0 else ""
    return f"{sign}₹{abs(val):,.0f}"

def pct(val):
    return f"{val*100:.2f}%"

# ─── UI ─────────────────────────────────────────────────────────────────────

st.title("📊 Partnership Tracker")
st.caption(f"Last refreshed: {datetime.now().strftime('%d %b %Y, %I:%M %p')}")

st.sidebar.header("⚙️ Settings")
if st.sidebar.button("🔄 Refresh Data"):
    st.cache_data.clear()
    st.rerun()

with st.sidebar.expander("🔧 Debug ERPNext"):
    st.caption("Phase 1 (single range) raw response:")
    st.json(st.session_state.get("erpnext_raw_phase1", {"info": "No response yet. Refresh data."}))
    st.caption("Phase 2 (monthly) raw response:")
    st.json(st.session_state.get("erpnext_raw_phase2", {"info": "No response yet. Refresh data."}))

events_df = get_capital_events()
phase2_start = get_phase2_start(events_df)
phase1_end = phase2_start - pd.Timedelta(days=1)
phase1_end = phase1_end.date() if hasattr(phase1_end, "date") else phase1_end
today = date.today()

erpnext_ok = True
try:
    with st.spinner("Fetching Phase 1 P&L (business start → day before partnership)..."):
        phase1_profit = get_pnl_for_range(PHASE1_START, phase1_end)
    with st.spinner("Fetching Phase 2 monthly P&L..."):
        pnl_map = get_monthly_pnl_map(phase2_start, today)
except Exception as e:
    st.warning(f"Could not fetch ERPNext P&L: {e}. Using zeros.")
    phase1_profit = 0.0
    pnl_map = {}
    erpnext_ok = False

# Capital entering Phase 2 = each partner's contributions up to phase2_start,
# with Ravi additionally credited the full Phase 1 profit (he earned it solo).
opening_capital = {p: capital_as_of(events_df, p, phase2_start) for p in PARTNERS}
opening_capital["Ravi"] += phase1_profit

phase2_results, closing_capital = compute_phase2(events_df, phase2_start, opening_capital, pnl_map)

total_shares = {"Ravi": phase1_profit, "Neeraj": 0.0, "Dharmesh": 0.0}
for row in phase2_results:
    for p in PARTNERS:
        total_shares[p] += row[f"{p} Share"]
total_allocated = sum(total_shares.values())

# ── Section 1: Current Value ──
st.subheader("💰 Current Value (What Each Partner Is Owed Today)")
st.caption(f"Based on data up to {today.strftime('%d %b %Y')}")

cols = st.columns(3)
for i, p in enumerate(PARTNERS):
    current_val = closing_capital.get(p, opening_capital[p])
    total_contrib = events_df[events_df["Partner"] == p]["Amount"].sum() if not events_df.empty else 0
    profit = total_shares[p]
    roi = (profit / total_contrib * 100) if total_contrib else 0
    with cols[i]:
        st.metric(
            label=p,
            value=inr(current_val),
            delta=f"{inr(profit)} profit ({roi:.1f}%) — {inr(total_contrib)} contributed"
        )

st.caption(
    f"Sum of all partners' profit: **{inr(total_allocated)}** — "
    f"should match ERPNext's total net profit since business start ({PHASE1_START.strftime('%d %b %Y')})."
)

st.divider()

# ── Section 2: Single combined monthly breakdown (Phase 1 lump + Phase 2 months) ──
st.subheader("📅 Monthly Breakdown")

first_row = {
    "Month": f"{PHASE1_START.strftime('%b %Y')} \u2013 {phase1_end.strftime('%d %b %Y')}",
    "Net Profit": phase1_profit,
    "Neeraj Ratio": 0.0, "Dharmesh Ratio": 0.0, "Ravi Ratio": 1.0,
    "Neeraj Share": 0.0, "Dharmesh Share": 0.0, "Ravi Share": phase1_profit,
    "Neeraj Capital A/C": opening_capital["Neeraj"],
    "Dharmesh Capital A/C": opening_capital["Dharmesh"],
    "Ravi Capital A/C": opening_capital["Ravi"],
}
all_rows = [first_row] + phase2_results

if all_rows:
    df_monthly = pd.DataFrame(all_rows)
    tabs = st.tabs(["Summary", "Ratios", "Capital Accounts"])

    with tabs[0]:
        summary = df_monthly[["Month", "Net Profit"] + [f"{p} Share" for p in PARTNERS]]
        fmt = {"Net Profit": inr, **{f"{p} Share": inr for p in PARTNERS}}
        st.dataframe(summary.style.format(fmt), use_container_width=True, hide_index=True)
        chart_df = df_monthly.set_index("Month")[[f"{p} Share" for p in PARTNERS]]
        chart_df.columns = PARTNERS
        # st.bar_chart(chart_df)

    with tabs[1]:
        ratio_cols = ["Month"] + [f"{p} Ratio" for p in PARTNERS]
        st.dataframe(df_monthly[ratio_cols].style.format({f"{p} Ratio": pct for p in PARTNERS}),
                     use_container_width=True, hide_index=True)

    with tabs[2]:
        acc_cols = ["Month"] + [f"{p} Capital A/C" for p in PARTNERS]
        st.dataframe(df_monthly[acc_cols].style.format({f"{p} Capital A/C": inr for p in PARTNERS}),
                     use_container_width=True, hide_index=True)
        line_df = df_monthly.set_index("Month")[[f"{p} Capital A/C" for p in PARTNERS]]
        line_df.columns = PARTNERS
        # st.line_chart(line_df)
else:
    st.info("No data yet.")

# st.divider()

# ── Section 4: Capital Events Log ──
st.subheader("📋 Capital Events Log")
if not events_df.empty:
    display_df = events_df.copy()
    display_df["Date"] = display_df["Date"].dt.strftime("%d %b %Y")
    display_df["Amount"] = display_df["Amount"].apply(inr)
    st.dataframe(display_df[["Date", "Partner", "Amount", "Notes"]],
                 use_container_width=True, hide_index=True)
else:
    st.info("No capital events found. Add entries to the Google Sheet.")

# ── Section 5: Reconciliation check ──
with st.expander("✅ Reconciliation Check"):
    st.write(f"Phase 1 profit (Ravi solo): {inr(phase1_profit)}")
    st.write(f"Phase 2 total profit (all months, all partners): {inr(total_allocated - phase1_profit)}")
    st.write(f"**Total allocated across app: {inr(total_allocated)}**")
    st.caption(
        "Compare this to ERPNext's Profit and Loss Statement for the full date range "
        f"({PHASE1_START.strftime('%d %b %Y')} \u2013 {today.strftime('%d %b %Y')}). "
        "If they don't match, check the ERPNext company/date filters."
    )