# AR & Collection Performance System

Streamlit application for generating AR/Collection reports with SQL Server data, including:
- AR-related collection summary and row-detailed reports
- Sales-related collection views
- Target and overdue analysis
- CSV/Excel export options
- LDAP-based login/authentication

## Tech Stack
- Python 3.10+
- Streamlit
- Pandas
- SQLAlchemy
- pyodbc (SQL Server driver)
- ldap3

## Project Structure
- `Direct_Sales_Collection_Report_Streamlit.py` — main Streamlit app
- `.streamlit/secrets.toml` — local secrets file (ignored by git)
- `.gitignore` — excludes secrets/logs/cache/venv from repository

## Prerequisites
1. Install Python dependencies:
   ```bash
   pip install streamlit pandas sqlalchemy pyodbc ldap3 python-dateutil plotly openpyxl xlsxwriter
   ```
2. Install **ODBC Driver 18 for SQL Server** on your machine.
3. Ensure network access to your SQL Server and LDAP server.

## Secure Configuration (Required)

Create a local file: `.streamlit/secrets.toml`

```toml
[db]
host = "YOUR_SQL_HOST\\INSTANCE"
user = "YOUR_SQL_USER"
password = "YOUR_SQL_PASSWORD"
database = "RXTracking"
driver = "ODBC Driver 18 for SQL Server"
trust_server_certificate = "yes"
```

### Optional Environment Variable Fallback
If `secrets.toml` is not used, the app can read:
- `DB_HOST`
- `DB_USER`
- `DB_PASSWORD`
- `DB_DATABASE`
- `DB_DRIVER`
- `DB_TRUST_SERVER_CERTIFICATE`

## Run the App
```bash
streamlit run Direct_Sales_Collection_Report_Streamlit.py
```

## GitHub Safety Checklist
- Never commit `.streamlit/secrets.toml`
- Rotate credentials before/after publishing if previously exposed
- Keep `.gitignore` intact
- Avoid hardcoding credentials in source code

## Notes
- The app uses SQL stored procedures for report generation.
- Some features depend on internal network resources (SQL/LDAP), so they may not run outside your environment.
