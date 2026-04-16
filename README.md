# AR & Collection Performance System

A Streamlit-based reporting application for AR and Collection monitoring. It generates multiple operational views from SQL Server stored procedures, supports interactive adjustments (where applicable), and provides downloads for analysis and manual checking.

## Key Features

### Authentication & Access
- LDAP/Active Directory login (NTLM) with access-level detection (e.g., Manager/User based on group membership).
- Built-in lockout protection after repeated failed attempts (configurable in code).

### AR & Collection Reporting (AR Related)
- AR-related collection dashboard with multi-tab layout.
- “Summarize Collection” view for month-level collection output.
- “Row-Detailed Collection” view for detailed transaction-level collection output.

### Sales-Related Reporting
- Sales-related collection reporting and financial overview tiles (paid/unpaid, overdue counts, max exposures).
- Multiple sales-related analytical tabs (summary, incentives-related views, overdue summaries, rebates summaries).

### Target / Aging / Overdue Analysis
- Aging bucket computation and “Collection Performance” view (Current / 1–30 / 31–60 / 61–90 / 91+ plus totals).
- Overdue reporting with filtering and downloadable outputs.

### Add-Days (Due Date Adjustment) Tools
- “AR with Add Days” modal view for reviewing due-date adjustments.
- Uses default Add-Days mapping from the database plus UI-driven overrides.
- Supports maintaining a list of customer names with custom add-days and applying them to the dataset logic used for aging.

### Customizable Conditions (No Code Changes Needed)
- Condition sets are user-maintainable and can be edited through the app UI:
  - CO conditions
  - COD conditions
- These condition sets drive category/assignment behavior inside the application (e.g., target categorization, DSS2/category mapping, and related rule-based labeling), allowing business-rule updates without modifying Python source code.

### Re-Tagging & History
- Re-tagging module for updating assigned labels (e.g., SR2 / DSS-related fields) from the UI.
- History tracking and replay:
  - View, edit, and delete re-tag history entries.
  - Apply saved history to the current dataset to enforce consistent tagging across runs.

### Data Export
- One-click CSV downloads across major tables/views.
- Excel download support where available (with fallback behavior when Excel writer is missing).
- Designed to safely display tables in Streamlit (including date/datetime handling for dataframe rendering).

## Tech Stack
- Python
- Streamlit
- Pandas / NumPy
- SQLAlchemy + pyodbc (SQL Server)
- ldap3 (LDAP/AD authentication)
- Plotly (charts/visuals)

## Requirements
- Python 3.10+ recommended
- SQL Server ODBC driver installed:
  - ODBC Driver 18 for SQL Server

Install dependencies (example):
```bash
pip install streamlit pandas sqlalchemy pyodbc ldap3 python-dateutil plotly openpyxl xlsxwriter
```

## Secure Configuration (For GitHub)

This project is intended to be published without exposing credentials.

### Database Connection (Secrets / Environment)
The application reads DB connection settings from Streamlit Secrets (preferred) or environment variables (fallback). Do not hardcode credentials in the repository.

Recommended: create a local `.streamlit/secrets.toml` (ignored by git) and define the DB fields there.

Environment variable fallback:
- DB_HOST
- DB_USER
- DB_PASSWORD
- DB_DATABASE
- DB_DRIVER (optional)
- DB_TRUST_SERVER_CERTIFICATE (optional)

### Git Ignore
Keep secret/config files out of Git history. This repo is configured to ignore:
- `.streamlit/secrets.toml`
- `.env` files
- common log outputs and caches

## Run
```bash
streamlit run Direct_Sales_Collection_Report_Streamlit.py
```

## Operational Notes
- Some modules depend on internal network resources (SQL Server and LDAP). Running outside the intended environment may require updating network endpoints and identity settings.
- Report outputs rely on SQL stored procedures; ensure the expected stored procedures and permissions exist in the target database.
- For best results, confirm the correct SQL Server ODBC driver is installed and accessible to Python/pyodbc.

## License

Copyright (c) 2026 InnoGen Pharmaceuticals Inc. All rights reserved.

Author/Maintainer: Benedic Cater

This software is proprietary and confidential. No part of this repository may be copied, modified, published, distributed, or used to create derivative works without prior written permission from InnoGen Pharmaceuticals Inc.

For internal use only.
