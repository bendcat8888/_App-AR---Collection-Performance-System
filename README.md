# 📊 AR & Collection Performance System
**Enterprise-Grade Financial Analytics & Accounts Receivable Management Solution**

[![Python 3.12](https://img.shields.io/badge/Python-3.12-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/Streamlit-FF4B4B?style=for-the-badge&logo=Streamlit&logoColor=white)](https://streamlit.io/)
[![SQL Server](https://img.shields.io/badge/SQL%20Server-CC2927?style=for-the-badge&logo=microsoft-sql-server&logoColor=white)](https://www.microsoft.com/en-us/sql-server)

## 🛠 Tech Stack

| Category | Tools |
| :--- | :--- |
| **Language** | Python 3.12 (Advanced Type Hinting & Caching) |
| **Frontend** | Streamlit (Custom CSS & Multi-tab Architecture) |
| **Database** | MS SQL Server (via SQLAlchemy & ODBC 18) |
| **Auth** | LDAP3 (Active Directory / NTLM) |
| **Analysis** | Pandas, NumPy, python-dateutil |
| **Visuals** | Plotly Interactive Graphics |

---

## 🎯 Executive Summary
A sophisticated **Collection Performance System** designed for the pharmaceutical industry (InnoGen). This application bridges the gap between raw SQL Server financial data and executive decision-making. It automates complex AR aging calculations, manages LDAP-secured access, and allows non-technical users to update business logic (CO/COD conditions) directly through a high-performance Streamlit UI.

---

## 🚀 Key Modules & Professional Capabilities

### 🔐 Enterprise Security & Identity
* **Active Directory Integration:** Implemented secure **LDAP/NTLM authentication** using `ldap3`, featuring role-based access control (RBAC) for Managers vs. Users.
* **Brute-Force Protection:** Built-in lockout mechanisms and secure session handling to meet corporate security audit requirements.

### 📈 Advanced Financial Analytics
* **Dynamic AR Aging:** Real-time computation of aging buckets (Current, 1–30, 31–60, 61–90, 91+).
* **Predictive Collection Tools:** "Add-Days" simulation module allowing users to review and override due-date adjustments based on custom customer-mapping logic.
* **Sales Performance Tiles:** High-level financial KPIs including max exposure, overdue counts, and incentive-related summaries.

### ⚙️ No-Code Business Logic Management
* **Dynamic Condition Sets:** Empowered business users to modify **CO/COD conditions** and **SR2/DSS re-tagging** via the UI.
* **Logic Persistence:** Changes are written back to the database or history logs, allowing rule-based labeling to evolve without modifying the Python source code.

### 🛠 Data Engineering & Reliability
* **SQL Optimization:** Leveraged `SQLAlchemy` and `pyodbc` to interface with complex Stored Procedures, ensuring minimal memory overhead and fast retrieval.
* **Audit Trails:** Re-tagging history module allows for "replay" functionality, ensuring data consistency across multiple reporting cycles.
* **Robust Exports:** Multi-format support (CSV/Excel) with specialized date-time handling for clean data ingestion into ERP systems.

---

## ⚙️ Requirements & Installation

- **Python:** 3.10+ recommended
- **Driver:** [ODBC Driver 18 for SQL Server](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server)

### Quick Start
```bash
# Install dependencies
pip install streamlit pandas sqlalchemy pyodbc ldap3 python-dateutil plotly openpyxl xlsxwriter

# Launch the application
streamlit run Direct_Sales_Collection_Report_Streamlit.py
```

## Operational Notes
- Some modules depend on internal network resources (SQL Server and LDAP). Running outside the intended environment may require updating network endpoints and identity settings.
- Report outputs rely on SQL stored procedures; ensure the expected stored procedures and permissions exist in the target database.
- For best results, confirm the correct SQL Server ODBC driver is installed and accessible to Python/pyodbc.

## License

### Copyright (c) 2026 Benedic Cater / InnoGen Pharmaceuticals Inc.

### All Rights Reserved.

This repository and its contents, including all code, assets, and data, are the sole property of the author. This code is made public for portfolio review and demonstration purposes only.

### Restrictions:
- You may not copy, modify, or distribute this code.
- You may not use the "InnoGen" name, branding, or logos for any purpose.
- Use of the data contained within this repository for commercial or personal projects is strictly prohibited.

For inquiries or permission requests, please contact the author.
