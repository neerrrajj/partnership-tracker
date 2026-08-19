import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import requests
from datetime import datetime, date
import calendar
import json

st.set_page_config(page_title="Partnership Tracker", page_icon="📊", layout="wide")

# ─── Auth ───────────────────────────────────────────────────────────────────

@st.cache_resource
def get_gsheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(st.secrets["gcp_service_account"], scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(st.secrets["google"]["sheet_id"])

@st.cache_data(ttl=300)
def get_capital_events():
    sh = get_gsheet()
    ws = sh.worksheet("Capital Events")
    df = pd.DataFrame(ws.get_all_records())
    if df.empty:
        df = pd.DataFrame(columns=["Date", "Partner", "Type", "Amount", "Notes"])
        df["Date"] = pd.to_datetime(df["Date"])
        return df
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True)
    df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce").fillna(0)
    df.loc[df["Type"] == "Withdrawal", "Amount"] *= -1
    df = df.sort_values("Date").reset_index(drop=True)
    return df

@st.cache_data(ttl=300)
def get_erpnext_pnl(from_date: str, to_date: str):
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
        "from_fiscal_year": str(pd.to_datetime(from_date).year),
        "to_fiscal_year": str(pd.to_datetime(to_date).year),
        "periodicity": "Monthly",
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
    raw = resp.json()
    data = raw.get("message", {})
    # Debug: store raw response for sidebar inspection
    st.session_state["erpnext_raw"] = raw

    # Find net profit row — handle both dict rows and list rows
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
                fieldname = col.get("fieldname", "") if isinstance(col, dict) else ""
                if fieldname and fieldname != "account":
                    if isinstance(row, dict):
                        period_vals[fieldname] = row.get(fieldname, 0) or 0
                    elif isinstance(row, list):
                        # find column index
                        for idx, c in enumerate(cols):
                            if isinstance(c, dict) and c.get("fieldname") == fieldname:
                                period_vals[fieldname] = row[idx] if idx < len(row) else 0
                                break
            return period_vals
    return {}

# ─── Core Calculation ───────────────────────────────────────────────────────

PARTNERS = ["Neeraj", "Dharmesh", "Ravi"]

@st.cache_data(ttl=300)
def get_opening_data():
    try:
        sh = get_gsheet()
        ws = sh.worksheet("Opening")
        df = pd.DataFrame(ws.get_all_records())
    except Exception:
        return {}
    if df.empty:
        return {}
    row = df.iloc[0].to_dict()
    for col in ["Receivables", "Inventory", "Ravi"]:
        if col in row:
            row[col] = pd.to_numeric(row[col], errors="coerce")
    return row

def get_opening_capitals():
    data = get_opening_data()
    ravi = (data.get("Receivables", 0) or 0) + (data.get("Inventory", 0) or 0) + (data.get("Ravi", 0) or 0)
    return {
        "Neeraj": 0,
        "Dharmesh": 0,
        "Ravi": ravi,
    }

def get_opening_date():
    data = get_opening_data()
    return pd.to_datetime(data.get("Date", ""), dayfirst=True)

def weighted_avg_capital(events_df: pd.DataFrame, partner: str, month_start: date, month_end: date) -> float:
    opening = get_opening_capitals()
    opening_date = get_opening_date().date()
    month_start = pd.Timestamp(month_start).date()
    month_end = pd.Timestamp(month_end).date()

    # If this month ends before opening, no capital deployed
    if month_end < opening_date:
        return 0

    # Partners with opening capital (like Ravi) count from month start — they had capital all along.
    # Partners with zero opening count from opening_date — they joined on that date.
    effective_start = month_start if opening.get(partner, 0) > 0 else max(month_start, opening_date)

    # Build running balance up to effective_start
    prior = events_df[
        (events_df["Partner"] == partner) &
        (events_df["Date"].dt.date < effective_start)
    ]
    balance = opening.get(partner, 0) + prior["Amount"].sum()

    # Events within this month from effective_start onwards
    month_events = events_df[
        (events_df["Partner"] == partner) &
        (events_df["Date"].dt.date >= effective_start) &
        (events_df["Date"].dt.date <= month_end)
    ].sort_values("Date")

    # Build segments
    segments = []
    cursor = effective_start
    for _, row in month_events.iterrows():
        event_date = row["Date"].date()
        if event_date > cursor:
            segments.append((balance, (event_date - cursor).days))
        balance += row["Amount"]
        cursor = event_date
    # Remaining days
    remaining = (month_end - cursor).days + 1
    segments.append((balance, remaining))

    total_days = (month_end - month_start).days + 1
    wtd = sum(bal * days for bal, days in segments) / total_days
    return wtd

def compute_monthly_allocations(events_df: pd.DataFrame, pnl_map: dict):
    """
    pnl_map: {period_key: net_profit_value}
    Returns list of monthly dicts with weighted capitals, ratios, shares, capital accounts
    """
    opening = get_opening_capitals()
    capital_accounts = {p: opening[p] for p in PARTNERS}
    results = []

    opening_date = get_opening_date().date()
    today = date.today()

    # Generate all months from opening date to today
    current_month_start = opening_date.replace(day=1)
    while current_month_start <= today:
        last_day = calendar.monthrange(current_month_start.year, current_month_start.month)[1]
        month_end_full = current_month_start.replace(day=last_day)
        month_end = month_end_full
        # Cap current month at today
        if current_month_start.year == today.year and current_month_start.month == today.month:
            month_end = today
        month_label = current_month_start.strftime("%b %Y")
        # Mark incomplete month
        if month_end < month_end_full:
            days_elapsed = (month_end - current_month_start).days + 1
            month_label = f"{month_label} ({days_elapsed}/{last_day})"

        # Look up P&L for this month from ERPNext data
        net_profit = 0
        for period_key, profit in pnl_map.items():
            if period_key == "total":
                continue
            try:
                # Try standard date format first (e.g. 2026-04-01)
                period_date = pd.to_datetime(period_key)
            except Exception:
                try:
                    # ERPNext monthly columns use format like "apr_2026"
                    period_date = pd.to_datetime(period_key, format="%b_%Y")
                except Exception:
                    continue
            if period_date.year == current_month_start.year and period_date.month == current_month_start.month:
                net_profit = profit
                break

        wtd = {p: weighted_avg_capital(events_df, p, current_month_start, month_end) for p in PARTNERS}
        total_wtd = sum(wtd.values())

        shares = {}
        for p in PARTNERS:
            ratio = wtd[p] / total_wtd if total_wtd else 0
            shares[p] = net_profit * ratio
            capital_accounts[p] += shares[p]
            # Add contributions/withdrawals for this month
            month_contrib = events_df[
                (events_df["Partner"] == p) &
                (events_df["Date"] >= pd.Timestamp(current_month_start)) &
                (events_df["Date"] <= pd.Timestamp(month_end))
            ]["Amount"].sum()
            capital_accounts[p] += month_contrib

        results.append({
            "Month": month_label,
            "Net Profit": net_profit,
            **{f"{p} Wtd Capital": wtd[p] for p in PARTNERS},
            **{f"{p} Ratio": (wtd[p] / total_wtd if total_wtd else 0) for p in PARTNERS},
            **{f"{p} Share": shares[p] for p in PARTNERS},
            **{f"{p} Capital A/C": capital_accounts[p] for p in PARTNERS},
        })

        # Advance to next month
        if current_month_start.month == 12:
            current_month_start = current_month_start.replace(year=current_month_start.year + 1, month=1)
        else:
            current_month_start = current_month_start.replace(month=current_month_start.month + 1)

    return results, {p: capital_accounts[p] for p in PARTNERS}

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
    if "erpnext_raw" in st.session_state:
        st.json(st.session_state["erpnext_raw"])
    else:
        st.caption("No response yet. Refresh data.")

opening_date = get_opening_date()
from_date = opening_date.replace(day=1).date()  # First day of opening month
to_date = date.today()

# Load data
with st.spinner("Loading capital events..."):
    events_df = get_capital_events()

with st.spinner("Fetching P&L from ERPNext..."):
    try:
        pnl_map = get_erpnext_pnl(str(from_date), str(to_date))
        erpnext_ok = True
    except Exception as e:
        st.warning(f"Could not fetch ERPNext P&L: {e}. Using empty P&L.")
        pnl_map = {}
        erpnext_ok = False

# Compute
monthly_data, closed_accounts = compute_monthly_allocations(events_df, pnl_map)

# ── Section 1: Current Value ──
st.subheader("💰 Current Value (What Each Partner Is Owed Today)")
st.caption(f"Based on data up to {to_date.strftime('%d %b %Y')}")

opening = get_opening_capitals()
cols = st.columns(3)
for i, p in enumerate(PARTNERS):
    current_val = closed_accounts.get(p, opening[p])
    total_contrib = events_df[events_df["Partner"] == p]["Amount"].sum() if not events_df.empty else 0
    profit = current_val - opening[p] - total_contrib
    deployed = opening[p] + total_contrib
    roi = (profit / deployed * 100) if deployed else 0
    with cols[i]:
        st.metric(
            label=p,
            value=inr(current_val),
            delta=f"{inr(profit)} profit ({roi:.1f}%) — {inr(deployed)} deployed"
        )

st.divider()

# ── Section 2: Capital Accounts (closed months) ──
st.subheader("🏦 Capital Accounts — Closed Months")
opening = get_opening_capitals()
cap_data = []
for p in PARTNERS:
    display_cap = closed_accounts.get(p, opening[p])
    total_contrib = events_df[events_df["Partner"] == p]["Amount"].sum() if not events_df.empty else 0
    profit = display_cap - opening[p] - total_contrib
    deployed = opening[p] + total_contrib
    cap_data.append({
        "Partner": p,
        "Opening Capital": inr(opening[p]),
        "Contributions": inr(total_contrib),
        "Profit": inr(profit),
        "Current Capital A/C": inr(display_cap),
        "ROI": pct(profit / deployed) if deployed else "—",
    })
st.dataframe(pd.DataFrame(cap_data).set_index("Partner"), use_container_width=True)

st.divider()

# ── Section 3: Monthly Breakdown ──
st.subheader("📅 Monthly Breakdown")

if monthly_data:
    tabs = st.tabs(["Summary", "Profit Shares", "Weighted Capitals", "Ratios", "Capital Accounts"])

    df_monthly = pd.DataFrame(monthly_data)

    with tabs[0]:
        summary = df_monthly[["Month", "Net Profit"] + [f"{p} Share" for p in PARTNERS]]
        fmt = {"Net Profit": inr, **{f"{p} Share": inr for p in PARTNERS}}
        st.dataframe(summary.style.format(fmt), use_container_width=True, hide_index=True)

    with tabs[1]:
        share_cols = ["Month"] + [f"{p} Share" for p in PARTNERS]
        share_df = df_monthly[share_cols].copy()
        st.dataframe(share_df.style.format({f"{p} Share": inr for p in PARTNERS}),
                     use_container_width=True, hide_index=True)

        # Bar chart
        chart_df = df_monthly.set_index("Month")[[f"{p} Share" for p in PARTNERS]]
        chart_df.columns = PARTNERS
        st.bar_chart(chart_df)

    with tabs[2]:
        wtd_cols = ["Month"] + [f"{p} Wtd Capital" for p in PARTNERS]
        wtd_df = df_monthly[wtd_cols].copy()
        st.dataframe(wtd_df.style.format({f"{p} Wtd Capital": inr for p in PARTNERS}),
                     use_container_width=True, hide_index=True)

    with tabs[3]:
        ratio_cols = ["Month"] + [f"{p} Ratio" for p in PARTNERS]
        ratio_df = df_monthly[ratio_cols].copy()
        st.dataframe(ratio_df.style.format({f"{p} Ratio": pct for p in PARTNERS}),
                     use_container_width=True, hide_index=True)

    with tabs[4]:
        acc_cols = ["Month"] + [f"{p} Capital A/C" for p in PARTNERS]
        acc_df = df_monthly[acc_cols].copy()
        st.dataframe(acc_df.style.format({f"{p} Capital A/C": inr for p in PARTNERS}),
                     use_container_width=True, hide_index=True)

        line_df = df_monthly.set_index("Month")[[f"{p} Capital A/C" for p in PARTNERS]]
        line_df.columns = PARTNERS
        st.line_chart(line_df)
else:
    st.info("No monthly data available.")

st.divider()

# ── Section 4: Capital Events Log ──
st.subheader("📋 Capital Events Log")
if not events_df.empty:
    display_df = events_df.copy()
    display_df["Date"] = display_df["Date"].dt.strftime("%d %b %Y")
    # Show original amount (before sign flip)
    display_df["Amount"] = display_df["Amount"].abs().apply(lambda x: inr(x))
    st.dataframe(display_df[["Date", "Partner", "Type", "Amount", "Notes"]], 
                 use_container_width=True, hide_index=True)
else:
    st.info("No capital events found. Add entries to the Google Sheet.")

# ── Section 5: Current Month Weighted Capitals ──
with st.expander("🔍 Current Month Weighted Capital Details"):
    today = date.today()
    month_start = today.replace(day=1)
    last_day = calendar.monthrange(today.year, today.month)[1]
    month_end = today.replace(day=last_day)
    live_wtd = {p: weighted_avg_capital(events_df, p, month_start, month_end) for p in PARTNERS}
    total = sum(live_wtd.values())
    rows = []
    for p in PARTNERS:
        rows.append({
            "Partner": p,
            "Weighted Capital": inr(live_wtd[p]),
            "Ratio": pct(live_wtd[p] / total if total else 0),
        })
    st.dataframe(pd.DataFrame(rows).set_index("Partner"), use_container_width=True)
    st.caption(f"Based on {(today - month_start).days + 1} days elapsed out of {last_day} days in current month.")