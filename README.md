# Partnership Tracker

Streamlit app to track capital accounts, profit shares, and ROI for a 3-partner stockist business.

## Setup

### 1. Google Sheet
Create a Google Sheet with one sheet named **"Capital Events"** with these columns:
```
Date | Partner | Type | Amount | Notes
```
- Date: DD-MM-YYYY
- Partner: exactly "Neeraj", "Other Friend", or "Business Owner"
- Type: "Contribution" or "Withdrawal"
- Amount: always positive
- Notes: optional

### 2. Google Service Account
1. Go to Google Cloud Console → Create a project
2. Enable Google Sheets API and Google Drive API
3. Create a Service Account → Download JSON key
4. Share your Google Sheet with the service account email (Editor access)
5. Copy the JSON fields into secrets.toml

### 3. ERPNext API Key
1. In ERPNext: Settings → My Profile → API Access → Generate Keys
2. Copy API Key and API Secret into secrets.toml

### 4. Streamlit Cloud
1. Push this repo to GitHub
2. Go to share.streamlit.io → New app → Connect repo
3. In App Settings → Secrets, paste the contents of secrets.toml
4. Deploy

## How it works
- Capital events are logged manually in Google Sheets (the only manual input)
- Net profit is pulled automatically from ERPNext P&L API each month
- Weighted average capital is computed in Python from the events log
- All profit splits, capital accounts, and ROI are calculated automatically