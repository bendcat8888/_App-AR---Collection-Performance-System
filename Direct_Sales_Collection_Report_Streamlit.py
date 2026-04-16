import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
from sqlalchemy import create_engine, text
import urllib.parse 
import os
from datetime import datetime, timedelta, date
from dateutil.relativedelta import relativedelta
import re
import base64
from io import BytesIO
import plotly.express as px
import plotly.graph_objects as go
from ldap3 import Server, Connection, NTLM, ALL, SUBTREE
import numpy as np  # noqa: F401
from st_aggrid import AgGrid, GridOptionsBuilder  # noqa: F401
from collections import Counter
import time
import logging

st.cache_data.clear()
st.cache_resource.clear()

if 'status_text' not in st.session_state:
    st.session_state.status_text = None

pd.options.mode.chained_assignment = 'raise'
# Set page layout to wide for better table display and add favicon
st.set_page_config(
    page_title="Collection Report System",
    page_icon="favicon.png",
    layout="wide"
)

# Configure logging at the module level
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('login_logs.txt', mode='a', encoding='utf-8'),  # Append to file with UTF-8
        logging.StreamHandler()  # Also print to console for development/debugging
    ]
)

logger = logging.getLogger(__name__)

# --- SR_CODE2 TROUBLESHOOTING: Set to True to enable debug UI and logs ---
DEBUG_SR_CODE2 = False


def _sr2_debug_log(msg):
    """Write to sr_code2_debug.log when DEBUG_SR_CODE2 is True. Remove with DEBUG_SR_CODE2."""
    if not DEBUG_SR_CODE2:
        return
    try:
        import os
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sr_code2_debug.log')
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f"{datetime.now().isoformat()} | {msg}\n")
    except Exception:
        pass


def _get_config_value(secrets_section, key, env_key, default=None, required=True):
    try:
        section = st.secrets.get(secrets_section, {})
        if isinstance(section, dict):
            val = section.get(key)
        else:
            val = None
    except Exception:
        val = None
    if val is None or str(val).strip() == "":
        val = os.getenv(env_key, default)
    if required and (val is None or str(val).strip() == ""):
        raise KeyError(f"Missing required configuration: [{secrets_section}].{key} or env var {env_key}")
    return val


# Columns that must display as whole numbers (no comma, no decimals): entry IDs and day counts
_NUMERIC_COLUMNS_WHOLE = {'Entry No_', 'EntryNo', 'Closed by Entry No_', 'ADD Days', 'AgingDays'}

# Known numeric columns (amounts, days, etc.) - always apply accounting format even if dtype is object (e.g. due to None)
_NUMERIC_COLUMNS_ACCOUNTING = {
    'Balance Due', 'Current', 'Days_1_to_30', 'Days_31_to_60', 'Days_61_to_90', 'Over_91_Days', 'Total Target',
    'Remaining Balance', 'BalanceDue',
    'Overdue_Amount', 'Current_Amount', 'COD_Amount', 'TOTAL_TARGET',
    'Collected_Amount', 'CollectedAmount', 'Collected_Return', 'Collected_EWT',
    'DetailAmount', 'Amount', 'remaining_balance', 'remaining balance',
    'Overdue Amount (PHP)', 'Average Days Overdue'
}


def _numeric_column_config(df, existing_config=None):
    """Build column_config with NumberColumn: whole-number format for Entry No_/ADD Days, accounting for amounts. Merge with existing_config."""
    config = dict(existing_config) if existing_config else {}
    for col in df.columns:
        if col in config:
            continue
        # Whole-number columns: no comma, no decimals
        if col in _NUMERIC_COLUMNS_WHOLE:
            config[col] = st.column_config.NumberColumn(col, format="%.0f")
            continue
        # Include if dtype is numeric, or if column is in known numeric list (handles None/object dtype)
        if pd.api.types.is_numeric_dtype(df[col]) or col in _NUMERIC_COLUMNS_ACCOUNTING:
            config[col] = st.column_config.NumberColumn(col, format="accounting")
    return config


# Initialize failed attempts tracker if not present (run this early in your app, e.g., before login_form)
if 'failed_attempts' not in st.session_state:
    st.session_state.failed_attempts = {}  # Dict: {username: {'count': int, 'timestamp': float}}
if 'max_attempts' not in st.session_state:
    st.session_state.max_attempts = 3
if 'lockout_duration' not in st.session_state:
    st.session_state.lockout_duration = 30 * 60  # 30 minutes in seconds

#### BACK TO ST.DIALOG
@st.fragment
def config_fragment(target):
    """Fragment for editing config conditions - can be used in dialog or standalone."""
    st.write("Edit the config values below. Changes will be saved.")
    
    # Load the appropriate CSV file
    if target == "CO":
        csv_file = 'CO_Conditions.csv'
        config_df = pd.read_csv(csv_file)                
    elif target == "COD":
        csv_file = 'COD_Conditions.csv'
        config_df = pd.read_csv(csv_file)
    else:
        st.error(f"Invalid target: {target}")
        return
        
    # Initialize session state key for this specific target
    state_key = f"config_df_{target}"
    if state_key not in st.session_state:
        st.session_state[state_key] = config_df.copy()
    
    # Use the session state data as input
    col_cfg = _numeric_column_config(st.session_state[state_key])
    edited_df = st.data_editor(
        st.session_state[state_key],
        hide_index=False,
        use_container_width=True,
        key=f"config_editor_{target}",  # Unique key for each target
        disabled=['Target_Category_Name', 'DSS2_Name'],
        column_config=col_cfg
    )
    
    # Update session state with edited data
    st.session_state[state_key] = edited_df.copy()
    
    # Save button to write changes to CSV
    col1, col2, col3 = st.columns([1, 1, 3])
    with col1:
        if st.button("💾 Save Changes", key=f"save_config_{target}", type="primary"):
            try:
                # Save to CSV file
                edited_df.to_csv(csv_file, index=False)
                st.success(f"Changes saved to {csv_file} successfully!")
                # Update session state to reflect saved changes
                st.session_state[state_key] = edited_df.copy()
            except Exception as e:
                st.error(f"Error saving to {csv_file}: {str(e)}")
    
    with col2:
        if st.button("🔄 Reset", key=f"reset_config_{target}"):
            # Reload from CSV file
            st.session_state[state_key] = pd.read_csv(csv_file).copy()
            st.success(f"Reset to original values from {csv_file}")

@st.dialog(title="Edit Conditions", width="large", dismissible=True)
def config_modal_fragment(target):
    """Dialog wrapper for single target editing (backward compatibility)."""
    config_fragment(target)

@st.dialog(title="Edit Conditions", width="large", dismissible=True)
def edit_conditions_modal_fragment():
    """Dialog to edit both CO and COD conditions in tabs."""
    tab1, tab2 = st.tabs(["CO Conditions", "COD Conditions"])
    
    with tab1:
        config_fragment("CO")
    
    with tab2:
        config_fragment("COD")

@st.dialog(title="Current Bucket Customers", width="large", dismissible=True)
def view_current_bucket_customers():
    """Display the list of customers that are kept in Current bucket."""
    try:
        current_bucket_df = pd.read_csv('Current_Bucket_Customers.csv')
        st.write("List of customers that are automatically kept in the Current aging bucket:")
        col_cfg = _numeric_column_config(current_bucket_df)
        st.dataframe(
            current_bucket_df,
            hide_index=False,
            use_container_width=True,
            column_config=col_cfg
        )
        st.info(f"Total customers in Current bucket list: {len(current_bucket_df)}")
    except FileNotFoundError:
        st.error("Current_Bucket_Customers.csv file not found.")
    except Exception as e:
        st.error(f"Error reading Current_Bucket_Customers.csv: {e}")

@st.dialog(title="AR with Add Days", width="large", dismissible=True)   
def AR_with_Add_Days_modal_fragment():
    AR_with_Add_Days_Normal_fragment()  # Call the new fragment function to render content in the dialog
    #### END

@st.dialog(title="Re-tagging SR / CR Module", width="large", dismissible=True)
def retagging_modal_fragment():
    retagging_fragment()  # Call the fragment function to render content in the dialog

@st.dialog(title="Default Customer with Add Days", width="large", dismissible=True)
def default_customer_add_days_modal_fragment():
    """Dialog modal to display the original list from sproc8a (sp_AR_AddDays)."""
    st.markdown("##### Default Customer with Add Days (from sp_AR_AddDays)")
    if 'result_df8a' not in st.session_state:
        st.warning("Add Days data not available. Please generate report first.")
        return
    df8a = st.session_state.result_df8a.copy()
    if df8a.empty:
        st.info("No default customer add-days data from sproc8a.")
        return
    col_cfg = _numeric_column_config(df8a)
    st.dataframe(df8a, use_container_width=True, hide_index=True, column_config=col_cfg)

def _re_tag_history_dss_editor_on_change():
    """Callback to save Apply Global edits and row deletions back to re_tag_history_dss.csv."""
    if "re_tag_history_dss_editor" not in st.session_state:
        return
    if "re_tag_history_dss_df" not in st.session_state:
        return
    state = st.session_state["re_tag_history_dss_editor"]
    if isinstance(state, pd.DataFrame):
        edited_df = state.copy()
    else:
        edited_df = st.session_state.re_tag_history_dss_df.copy()
        deleted_rows = state.get("deleted_rows", [])
        if deleted_rows:
            for idx in sorted(deleted_rows, reverse=True):
                if 0 <= idx < len(edited_df):
                    edited_df = edited_df.drop(edited_df.index[idx])
            edited_df = edited_df.reset_index(drop=True)
        edited_rows = state.get("edited_rows", {})
        if edited_rows and "Apply Global" in edited_df.columns:
            for index, updates in edited_rows.items():
                if index >= len(edited_df):
                    continue
                if "Apply Global" in updates:
                    val = updates["Apply Global"]
                    if isinstance(val, bool):
                        edited_df.loc[edited_df.index[index], "Apply Global"] = val
                    elif isinstance(val, str):
                        edited_df.loc[edited_df.index[index], "Apply Global"] = val.strip().lower() in ("true", "1", "yes")
                    else:
                        edited_df.loc[edited_df.index[index], "Apply Global"] = bool(val)
        added_rows = state.get("added_rows", [])
        if added_rows:
            for row_data in added_rows:
                new_row = {k: v for k, v in row_data.items() if k in edited_df.columns}
                if new_row:
                    edited_df = pd.concat([edited_df, pd.DataFrame([new_row])], ignore_index=True)
    if "Apply Global" in edited_df.columns:
        edited_df["Apply Global"] = edited_df["Apply Global"].fillna(False).apply(
            lambda x: x if isinstance(x, bool) else (str(x).strip().lower() in ("true", "1", "yes"))
        )
    st.session_state.re_tag_history_dss_df = edited_df
    try:
        edited_df.to_csv("re_tag_history_dss.csv", index=False)
    except Exception as e:
        logging.warning(f"Could not save re_tag_history_dss.csv: {e}")


def _re_tag_history_editor_on_change():
    """Callback to save Apply Global edits and row deletions back to re_tag_history.csv."""
    if "re_tag_history_editor" not in st.session_state:
        return
    if "re_tag_history_df" not in st.session_state:
        return
    state = st.session_state["re_tag_history_editor"]
    # When num_rows="dynamic" or "delete", the state can be the edited DataFrame directly (with deletions applied)
    if isinstance(state, pd.DataFrame):
        edited_df = state.copy()
    else:
        edited_df = st.session_state.re_tag_history_df.copy()
        # Handle deleted rows first (remove by index, descending to preserve indices)
        deleted_rows = state.get("deleted_rows", [])
        if deleted_rows:
            for idx in sorted(deleted_rows, reverse=True):
                if 0 <= idx < len(edited_df):
                    edited_df = edited_df.drop(edited_df.index[idx])
            edited_df = edited_df.reset_index(drop=True)
        # Handle edited rows (Apply Global, DEPT CODE, and other edits)
        edited_rows = state.get("edited_rows", {})
        if edited_rows:
            for index, updates in edited_rows.items():
                if index >= len(edited_df):
                    continue
                if "Apply Global" in edited_df.columns and "Apply Global" in updates:
                    val = updates["Apply Global"]
                    if isinstance(val, bool):
                        edited_df.loc[edited_df.index[index], "Apply Global"] = val
                    elif isinstance(val, str):
                        edited_df.loc[edited_df.index[index], "Apply Global"] = val.strip().lower() in ("true", "1", "yes")
                    else:
                        edited_df.loc[edited_df.index[index], "Apply Global"] = bool(val)
                if "DEPT CODE" in edited_df.columns and "DEPT CODE" in updates:
                    val = updates["DEPT CODE"]
                    # Normalize empty/blank/None to "" for storage
                    if pd.isna(val) or (isinstance(val, str) and val.strip().lower() in ("", "blank", "none")):
                        edited_df.loc[edited_df.index[index], "DEPT CODE"] = ""
                    else:
                        edited_df.loc[edited_df.index[index], "DEPT CODE"] = val
        # Handle added rows (for num_rows="dynamic")
        added_rows = state.get("added_rows", [])
        if added_rows:
            for row_data in added_rows:
                new_row = {k: v for k, v in row_data.items() if k in edited_df.columns}
                if "DEPT CODE" in edited_df.columns and "DEPT CODE" not in new_row:
                    new_row["DEPT CODE"] = ""
                if new_row:
                    edited_df = pd.concat([edited_df, pd.DataFrame([new_row])], ignore_index=True)
    # Ensure Apply Global defaults to False for any NaN/empty before saving
    if "Apply Global" in edited_df.columns:
        edited_df["Apply Global"] = edited_df["Apply Global"].fillna(False).apply(
            lambda x: x if isinstance(x, bool) else (str(x).strip().lower() in ("true", "1", "yes"))
        )
    # Normalize DEPT CODE "blank"/"None"/NaN to "" before saving
    if "DEPT CODE" in edited_df.columns:
        edited_df["DEPT CODE"] = edited_df["DEPT CODE"].apply(
            lambda x: "" if (pd.isna(x) or (isinstance(x, str) and str(x).strip().lower() in ("", "blank", "none"))) else x
        )
    st.session_state.re_tag_history_df = edited_df
    try:
        edited_df.to_csv("re_tag_history.csv", index=False)
    except Exception as e:
        logging.warning(f"Could not save re_tag_history.csv: {e}")


@st.dialog(title="Re-tag History", width="large", dismissible=True, on_dismiss="rerun")
def re_tag_history_modal_fragment():
    """Dialog modal to view and edit re-tag history from re_tag_history.csv and re_tag_history_dss.csv."""
    st.markdown("##### Re-tag History")
    tab_sr, tab_dss = st.tabs(["SR", "DSS"])

    with tab_sr:
        history_file = 're_tag_history.csv'
        try:
            history_df = pd.read_csv(history_file)
            if history_df.empty:
                st.info("No re-tag history yet. Re-tag SR2 in the table above to record history.")
            else:
                if "Apply Global" not in history_df.columns:
                    history_df["Apply Global"] = False
                else:
                    history_df["Apply Global"] = history_df["Apply Global"].apply(
                        lambda x: str(x).strip().lower() in ("true", "1", "yes") if pd.notna(x) and str(x).strip() else False
                    )
                # Task 1: Add DEPT CODE column after Apply Global if not present
                if "DEPT CODE" not in history_df.columns:
                    history_df["DEPT CODE"] = ""
                # Normalize "blank", "None", NaN to empty string for display (empty cell, not literal word)
                history_df["DEPT CODE"] = history_df["DEPT CODE"].apply(
                    lambda x: "" if (pd.isna(x) or (isinstance(x, str) and str(x).strip().lower() in ("", "blank", "none"))) else x
                )
                # Ensure column order: ... Apply Global, DEPT CODE (DEPT CODE after Apply Global)
                cols = [c for c in history_df.columns if c not in ("Apply Global", "DEPT CODE")]
                if "Apply Global" in history_df.columns:
                    cols.append("Apply Global")
                if "DEPT CODE" in history_df.columns:
                    cols.append("DEPT CODE")
                history_df = history_df[[c for c in cols if c in history_df.columns]]
                st.session_state.re_tag_history_df = history_df.copy()
                # Task 2 & 5: DEPT CODE editable with dropdown; empty string as first option (empty/blank)
                editable_cols_modal = ["Apply Global", "DEPT CODE"]
                disabled_cols = [c for c in history_df.columns if c not in editable_cols_modal]
                dept_code_options = [""]
                if "display_df6_view" in st.session_state and not st.session_state.display_df6_view.empty:
                    dept_col = next((c for c in ["DEPT CODE", "dept_code"] if c in st.session_state.display_df6_view.columns), None)
                    if dept_col:
                        unique_depts = sorted(set(str(v).strip() for v in st.session_state.display_df6_view[dept_col].dropna().unique() if str(v).strip()))
                        dept_code_options = [""] + unique_depts
                column_cfg_sr = {
                    "Apply Global": st.column_config.CheckboxColumn("Apply Global", default=False),
                    "DEPT CODE": st.column_config.SelectboxColumn(
                        "DEPT CODE",
                        options=dept_code_options,
                        required=False,
                        help="When Apply Global=True: empty=apply to all; or select to match main table DEPT CODE only. Ignored when Apply Global=False."
                    )
                }
                column_cfg_sr = _numeric_column_config(st.session_state.re_tag_history_df, column_cfg_sr)
                st.caption("Use the built-in delete icon or select a row and press **Delete**/**Backspace** to remove it from history.")
                edited_df = st.data_editor(
                    st.session_state.re_tag_history_df,
                    use_container_width=True,
                    hide_index=True,
                    key="re_tag_history_editor",
                    on_change=_re_tag_history_editor_on_change,
                    disabled=disabled_cols,
                    num_rows="dynamic",
                    column_config=column_cfg_sr
                )
                if edited_df is not None and not edited_df.equals(st.session_state.re_tag_history_df):
                    st.session_state.re_tag_history_df = edited_df.copy()
                    try:
                        # Normalize legacy "blank" and NaN to "" in DEPT CODE before saving to CSV
                        save_df = edited_df.copy()
                        if "DEPT CODE" in save_df.columns:
                            save_df["DEPT CODE"] = save_df["DEPT CODE"].apply(
                                lambda x: "" if (pd.isna(x) or (isinstance(x, str) and str(x).strip().lower() in ("", "blank", "none"))) else x
                            )
                        save_df.to_csv("re_tag_history.csv", index=False)
                    except Exception as e:
                        logging.warning(f"Could not save re_tag_history.csv: {e}")
                st.markdown("---")
                if st.button("Apply Global", key="btn_apply_global_in_modal", help="Apply re-tag history (including Apply Global entries) to all AR data. Updates SR2 and SR_Code2 across all displays and state."):
                    if "display_df6_view" in st.session_state and not st.session_state.display_df6_view.empty:
                        updated_df = apply_re_tag_history_to_df(st.session_state.display_df6_view.copy())
                        updated_df = apply_re_tag_history_dss_to_df(updated_df)
                        st.session_state.display_df6_view = updated_df.copy()
                        st.session_state.display_df6_view_state = updated_df.copy()
                        if "display_df6s" in st.session_state and not st.session_state.display_df6s.empty:
                            st.session_state.display_df6s = apply_re_tag_history_dss_to_df(apply_re_tag_history_to_df(st.session_state.display_df6s.copy()))
                        st.success("Re-tag history applied. SR2 and SR_Code2 updated. Close this dialog to see the updated data.")
                    else:
                        st.warning("No AR data available. Please generate the report first.")
        except FileNotFoundError:
            st.info("No re-tag history yet. Re-tag SR2 in the table above to record history.")
        except Exception as e:
            st.warning(f"Could not load re-tag history: {e}")

    with tab_dss:
        history_file_dss = 're_tag_history_dss.csv'
        try:
            history_dss_df = pd.read_csv(history_file_dss)
            if history_dss_df.empty:
                st.info("No DSS re-tag history yet. Re-tag DSS_NAME in the table above to record history.")
            else:
                if "Apply Global" not in history_dss_df.columns:
                    history_dss_df["Apply Global"] = False
                else:
                    history_dss_df["Apply Global"] = history_dss_df["Apply Global"].apply(
                        lambda x: str(x).strip().lower() in ("true", "1", "yes") if pd.notna(x) and str(x).strip() else False
                    )
                st.session_state.re_tag_history_dss_df = history_dss_df.copy()
                disabled_cols_dss = [c for c in history_dss_df.columns if c != "Apply Global"]
                column_cfg_dss = {"Apply Global": st.column_config.CheckboxColumn("Apply Global", default=False)}
                column_cfg_dss = _numeric_column_config(st.session_state.re_tag_history_dss_df, column_cfg_dss)
                st.caption("Use the built-in delete icon or select a row and press **Delete**/**Backspace** to remove it from history.")
                edited_dss_df = st.data_editor(
                    st.session_state.re_tag_history_dss_df,
                    use_container_width=True,
                    hide_index=True,
                    key="re_tag_history_dss_editor",
                    on_change=_re_tag_history_dss_editor_on_change,
                    disabled=disabled_cols_dss,
                    num_rows="dynamic",
                    column_config=column_cfg_dss
                )
                if edited_dss_df is not None and not edited_dss_df.equals(st.session_state.re_tag_history_dss_df):
                    st.session_state.re_tag_history_dss_df = edited_dss_df.copy()
                    try:
                        edited_dss_df.to_csv("re_tag_history_dss.csv", index=False)
                    except Exception as e:
                        logging.warning(f"Could not save re_tag_history_dss.csv: {e}")
                st.markdown("---")
                if st.button("Apply Global", key="btn_apply_global_dss_in_modal", help="Apply DSS re-tag history (including Apply Global entries) to all AR data. Updates DSS_NAME and DSS across all displays and state."):
                    if "display_df6_view" in st.session_state and not st.session_state.display_df6_view.empty:
                        updated_df = apply_re_tag_history_to_df(st.session_state.display_df6_view.copy())
                        updated_df = apply_re_tag_history_dss_to_df(updated_df)
                        st.session_state.display_df6_view = updated_df.copy()
                        st.session_state.display_df6_view_state = updated_df.copy()
                        if "display_df6s" in st.session_state and not st.session_state.display_df6s.empty:
                            st.session_state.display_df6s = apply_re_tag_history_dss_to_df(apply_re_tag_history_to_df(st.session_state.display_df6s.copy()))
                        st.success("DSS re-tag history applied. DSS_NAME and DSS updated. Close this dialog to see the updated data.")
                    else:
                        st.warning("No AR data available. Please generate the report first.")
        except FileNotFoundError:
            st.info("No DSS re-tag history yet. Re-tag DSS_NAME in the table above to record history.")
        except Exception as e:
            st.warning(f"Could not load DSS re-tag history: {e}")

@st.fragment
def retagging_fragment():
    """Fragment for retagging SR2 and SR_Code2 in display_df6_view."""
    if 'display_df6_view' not in st.session_state or st.session_state.display_df6_view.empty:
        st.warning("No data available. Please load data first.")
        return
    
    # Get a copy of the dataframe (excluding grand total row if present)
    df = st.session_state.display_df6_view.copy()
    
    # Exclude grand total row if it exists (usually the last row)
    if len(df) > 0:
        # Check if last row might be a grand total (common pattern)
        data_rows = df.iloc[:-1].copy() if len(df) > 1 else df.copy()
    else:
        data_rows = df.copy()
    
    # Check if required columns exist
    if 'SR2' not in data_rows.columns:
        st.error("SR2 column not found in the dataframe.")
        return
    
    if 'SR_Code2' not in data_rows.columns and 'SR_CODE2' not in data_rows.columns:
        st.warning("SR_Code2 or SR_CODE2 column not found. Only SR2 will be updated.")
        sr_code2_col = None
    else:
        sr_code2_col = 'SR_CODE2' if 'SR_CODE2' in data_rows.columns else 'SR_Code2'
    
    # Get unique SR2 values (excluding NaN and empty strings)
    # Normalize for display but keep original for matching
    sr2_normalized = data_rows['SR2'].astype(str).str.strip()
    sr2_normalized = sr2_normalized.replace('nan', np.nan)
    unique_sr2 = sr2_normalized.dropna()
    unique_sr2 = unique_sr2[unique_sr2.astype(str).str.strip() != '']
    unique_sr2 = sorted(unique_sr2.unique())
    
    if len(unique_sr2) == 0:
        st.warning("No SR2 values found in the dataframe.")
        return
    
    st.markdown("### Re-tagging SR2 and SR_Code2")
    st.info("Select the SR2 to replace and the new SR2 name. All matching rows will be updated.")
    
    # Create two columns for selectboxes
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("#### Select SR2 to Replace")
        selected_sr2_old = st.selectbox(
            "Choose SR2 to replace:",
            unique_sr2,
            key="retagging_select_old_sr2",
            help="Select the SR2 value that you want to replace"
        )
    
    with col2:
        st.markdown("#### Select New SR2 Name")
        selected_sr2_new = st.selectbox(
            "Choose new SR2 name:",
            unique_sr2,
            key="retagging_select_new_sr2",
            help="Select the SR2 value that will replace the selected SR2"
        )
    
    # Check if old and new are the same
    if selected_sr2_old == selected_sr2_new:
        st.warning("Please select different SR2 values. The old and new SR2 cannot be the same.")
        return
    
    # Show preview of affected rows (use normalized comparison)
    sr2_normalized_for_match = data_rows['SR2'].astype(str).str.strip()
    sr2_normalized_for_match = sr2_normalized_for_match.replace('nan', np.nan)
    matching_rows = data_rows[sr2_normalized_for_match == str(selected_sr2_old).strip()]
    num_rows = len(matching_rows)
    
    if num_rows > 0:
        st.info(f"**{num_rows}** row(s) will be updated from '{selected_sr2_old}' to '{selected_sr2_new}'")
        
        # Find the corresponding SR_Code2 for the new SR2 (use normalized comparison)
        sr2_normalized_for_new = data_rows['SR2'].astype(str).str.strip()
        sr2_normalized_for_new = sr2_normalized_for_new.replace('nan', np.nan)
        new_sr2_rows = data_rows[sr2_normalized_for_new == str(selected_sr2_new).strip()]
        if sr_code2_col and len(new_sr2_rows) > 0:
            # Get the most common SR_Code2 value for the new SR2
            new_sr_code2_values = new_sr2_rows[sr_code2_col].dropna()
            if len(new_sr_code2_values) > 0:
                # Use mode() to get the most common value, fallback to first value if mode is empty
                new_sr_code2_mode = new_sr_code2_values.mode()
                if len(new_sr_code2_mode) > 0:
                    new_sr_code2_value = new_sr_code2_mode.iloc[0]
                else:
                    new_sr_code2_value = new_sr_code2_values.iloc[0]
                st.info(f"SR_Code2 will be updated to: **{new_sr_code2_value}**")
            else:
                new_sr_code2_value = None
                st.warning("No SR_Code2 value found for the new SR2. SR_Code2 will not be updated.")
        else:
            new_sr_code2_value = None
        
        # Apply button
        if st.button("Apply Re-tagging", key="retagging_apply_btn", type="primary"):
            try:
                # Create a working copy of the full dataframe
                updated_df = st.session_state.display_df6_view.copy()
                
                # Identify rows to update (excluding grand total)
                if len(updated_df) > 1:
                    data_rows_to_update = updated_df.iloc[:-1].copy()
                    grand_total = updated_df.iloc[-1:].copy()
                else:
                    data_rows_to_update = updated_df.copy()
                    grand_total = pd.DataFrame()
                
                # Normalize SR2 column for matching (strip whitespace, handle NaN)
                sr2_normalized = data_rows_to_update['SR2'].astype(str).str.strip()
                sr2_normalized = sr2_normalized.replace('nan', np.nan)
                
                # Create mask for matching rows - use normalized comparison
                # Convert both to string and strip for comparison
                mask = sr2_normalized == str(selected_sr2_old).strip()
                
                # Verify we have matches
                num_matches = mask.sum()
                if num_matches == 0:
                    st.error(f"No rows found matching SR2 = '{selected_sr2_old}'. Please check the value and try again.")
                    # Show some sample SR2 values for debugging
                    sample_sr2 = data_rows_to_update['SR2'].dropna().unique()[:5]
                    st.info(f"Sample SR2 values in dataframe: {list(sample_sr2)}")
                    return
                
                # Find the actual value in dataframe that matches the new SR2 (to preserve formatting)
                new_sr2_normalized = data_rows_to_update['SR2'].astype(str).str.strip()
                new_sr2_normalized = new_sr2_normalized.replace('nan', np.nan)
                new_sr2_mask = new_sr2_normalized == str(selected_sr2_new).strip()
                
                if new_sr2_mask.any():
                    # Get the first actual value from dataframe that matches
                    actual_new_sr2_value = data_rows_to_update.loc[new_sr2_mask, 'SR2'].iloc[0]
                else:
                    # If no match found, use the selectbox value
                    actual_new_sr2_value = selected_sr2_new
                
                # Update SR2 with the actual value from dataframe
                data_rows_to_update.loc[mask, 'SR2'] = actual_new_sr2_value
                
                # Update SR_Code2 if column exists and we have a new value
                if sr_code2_col and new_sr_code2_value is not None:
                    data_rows_to_update.loc[mask, sr_code2_col] = new_sr_code2_value
                
                # Reattach grand total if it existed
                if not grand_total.empty:
                    final_df = pd.concat([data_rows_to_update, grand_total], ignore_index=True)
                else:
                    final_df = data_rows_to_update
                
                # Apply category function to correct Category and DSS2_Name
                with st.spinner("Applying category conditions..."):
                    final_df = apply_category_to_display_df(final_df)
                
                # Verify the update before saving
                verify_mask = final_df['SR2'].astype(str).str.strip() == str(actual_new_sr2_value).strip()
                verify_count = verify_mask.sum() if len(final_df) > 1 else verify_mask.sum() - 1  # Exclude grand total
                
                # Update session state - ensure we're updating the actual dataframe
                st.session_state.display_df6_view = final_df.copy()
                
                st.success(f"✅ Successfully updated {num_matches} row(s) from '{selected_sr2_old}' to '{actual_new_sr2_value}'!")
                st.info(f"📊 Verification: {verify_count} row(s) now have SR2 = '{actual_new_sr2_value}'")
                st.info("💡 **Tip:** Close this dialog to see the updated data in the main dataframe.")
                st.balloons()
                
            except Exception as e:
                st.error(f"Error during re-tagging: {str(e)}")
                st.exception(e)
    else:
        st.warning(f"No rows found with SR2 = '{selected_sr2_old}'")
        
def scroll_top():         
    html = """     
    <style>     
    body {
        margin: 0;
        padding: 0;
        overflow: hidden;
    }
    .scroll-top-container {         
        position: fixed;         
        bottom: 25px;         
        right: 25px;         
        z-index: 9999;     
    }     
    .scroll-top-btn {         
        background-color: #16a34a;         
        color: white;         
        border: none;         
        border-radius: 50%;         
        width: 55px;         
        height: 55px;         
        font-size: 26px;         
        font-weight: bold;         
        cursor: pointer;         
        box-shadow: 0 4px 10px rgba(0,0,0,0.4);         
        transition: all 0.3s ease;     
    }     
    .scroll-top-btn:hover { 
        transform: scale(1.05);
        background-color: #15803d;
    }     
    </style> 
    
    <div class="scroll-top-container">  
        <button class="scroll-top-btn" id="scrollTopBtn">↑</button>     
    </div>      
    
    <script>     
    (function(){
      // Position the iframe itself fixed on the parent page
      try {
        const iframe = window.frameElement;
        if (iframe) {
          iframe.style.position = 'fixed';
          iframe.style.bottom = '0';
          iframe.style.right = '0';
          iframe.style.width = '100px';
          iframe.style.height = '100px';
          iframe.style.border = 'none';
          iframe.style.zIndex = '9999';
          iframe.style.background = 'transparent';
          // REMOVED: iframe.style.pointerEvents = 'none';
        }
      } catch(e) {
        console.log('Could not style iframe:', e);
      }
      
      const targetId = 'report-generator';
      function doScrollActions() {
        try {
          const parentLoc = window.parent.location;
          const base = parentLoc.href.split('#')[0];
          window.parent.history.replaceState(null, '', base + '#' + targetId);
          
          const pdoc = window.parent.document;
          const anchor = pdoc.getElementById(targetId) || pdoc.querySelector('[name="'+targetId+'"]');
          if (anchor && anchor.scrollIntoView) {
            anchor.scrollIntoView({ behavior: 'smooth', block: 'start' });
            setTimeout(() => { window.parent.scrollBy(0, -80); }, 350);
            return true;
          }
          
          const containers = pdoc.querySelectorAll('[data-testid="stAppViewContainer"], .main, section, .reportview-container, .streamlit-container');
          if (containers.length) {
            containers.forEach(c => { if (c.scrollTo) c.scrollTo({ top: 0, behavior: 'smooth' }); });
            return true;
          }
          
          window.parent.scrollTo({ top: 0, behavior: 'smooth' });
          return true;
        } catch (e) {
          return false;
        }
      }
      
      const btn = document.getElementById('scrollTopBtn');
      btn.addEventListener('click', function() {
        if (!doScrollActions()) {
          setTimeout(doScrollActions, 120);
          setTimeout(doScrollActions, 450);
          setTimeout(doScrollActions, 900);
        }
      });
    })();     
    </script>     
    """
    components.html(html, height=0, scrolling=False)

@st.fragment
def AR_with_add_days_fragment():
    # Data editor for displaying and editing the DataFrame
    col_title, col_btn = st.columns([4, 1])
    with col_title:
        st.header("A/R with Add Days for Collection Performance")
    with col_btn:
        if st.button("View A/R Add Days", key="btn_ar_with_add_days_collection", use_container_width=True, help="A/R with Add Days"):
            AR_with_Add_Days_modal_fragment()
    col_cfg = _numeric_column_config(st.session_state.display_df6s)
    st.data_editor(
        st.session_state.display_df6s,
        num_rows="fixed",
        key="data_editor_df6s",
        on_change=df_on_change2,
        disabled=['AgingDays', 'Current', 'Days_1_to_30', 'Days_31_to_60', 'Days_61_to_90', 'Over_91_Days', 'Total Target'],
        column_config=col_cfg
    )    # Main Table for Collection Performance (Aging Bucket)
    # Add download button for the latest data
    st.download_button(
        label="Download CSV",
        data=st.session_state.display_df6s.to_csv(index=False).encode('utf-8'),
        file_name=f"AR_for_Collection_data_{st.session_state.date_to_str}.csv",
        mime="text/csv"
    )
    
    # Button to view Current Bucket Customers list
    if st.button("View List of Customers stays in CURRENT Aging", key="view_current_bucket_btn"):
        view_current_bucket_customers()

# Hardcoded column rename and sequence for AR with Add Days Excel export
_AR_EXCEL_COLUMNS = [
    ('CUSTOMER NO.', 'Customer No_'),
    ('POSTING DATE', 'Posting Date'),
    ('DUE DATE', 'Original Due Date'),
    ('NAME', 'Name'),
    ('DESCRIPTION', 'Description'),
    ('CITY', 'City'),
    ('AREA NAME', 'AREA_NAME'),
    ('Gen. Bus. Posting Group', 'Gen_ Bus_ Posting Group'),
    ('DOCUMENT TYPE', 'DOCUMENT TYPE'),
    ('ADD', 'ADD Days'),
    ('PAYMENT TERMS', 'Payment_Terms'),
    ('CHANNEL', None),
    ('DOCUMENT NO.', 'Document No_'),
    ('EXTERNAL DOCUMENT NO.', 'External Document No_'),
    ('ENTRY NO.', 'Entry No_'),
    ('BALANCE DUE', 'Balance Due'),
    ('AsOf Date', 'AsOfDate'),
    ('AgingDays', 'AgingDays'),
    ('CURRENT', 'Current'),
    ('1-30 DAYS', 'Days_1_to_30'),
    ('31-60 DAYS', 'Days_31_to_60'),
    ('61-90 DAYS', 'Days_61_to_90'),
    ('Over 91 DAYS', 'Over_91_Days'),
    ('SKU', 'PRODUCT'),
    ('DEPT', 'DEPT CODE'),
    ('PMR', 'PMR_NAME'),
    ('DSM', 'DSM_NAME'),
    ('CR', 'CR_NAME'),
    ('DSS', 'DSS_NAME'),
    ('DSS2', 'DSS2_Name'),
    ('CR2', 'SR2'),
    ('CATEGORY', 'Category'),
    ('Turned over / Remarks', None),
]


def _prepare_ar_excel_df(df):
    """Rename and reorder columns for AR Excel export. Returns new DataFrame."""
    out_cols = []
    for output_name, original_name in _AR_EXCEL_COLUMNS:
        if original_name and original_name in df.columns:
            out_cols.append((output_name, df[original_name]))
        else:
            out_cols.append((output_name, pd.Series([''] * len(df), index=df.index)))
    return pd.DataFrame({name: s for name, s in out_cols})


@st.fragment
def AR_with_Add_Days_Normal_fragment():
    """Fragment to display AR data with Add Days merged from sproc8a and overwritten from display_df_name list."""
    # Prefer display_df6_view (main table) so default entries (e.g. Planet) and latest updates are included; fall back to display_df6_view_state
    source_df = None
    if 'display_df6_view' in st.session_state and not st.session_state.display_df6_view.empty:
        source_df = st.session_state.display_df6_view.copy()
    if source_df is None and 'display_df6_view_state' in st.session_state and not st.session_state.display_df6_view_state.empty:
        source_df = st.session_state.display_df6_view_state.copy()
    if source_df is None or source_df.empty:
        st.warning("No data available. Please load data first.")
        return
    
    if 'result_df8a' not in st.session_state:
        st.warning("Add Days data not available. Please generate report first.")
        return
    
    merged_df = source_df.copy()
    merged_df = blank_payment_terms_for_credit_payment(merged_df)
    
    # Ensure ADD Days column exists
    if 'ADD Days' not in merged_df.columns:
        merged_df['ADD Days'] = pd.NA
    
    # Step 1: Merge with df8a (result_df8a) matching CUSTOMER_NO
    # Following the process from line 4880-4890
    df8a = st.session_state.result_df8a.copy()
    if not df8a.empty and 'CUSTOMER_NO' in df8a.columns and 'ADD_DAYS' in df8a.columns:
        # Prepare df8a for merge - include all ADD_DAYS (including 888xx prefix codes for age-bucket maintenance)
        df8a_merge = df8a[['CUSTOMER_NO', 'ADD_DAYS']].copy().drop_duplicates()
        
        # Check if Customer No_ column exists in merged_df
        customer_col = None
        for col in ['Customer No_', 'Customer No', 'CUSTOMER_NO', 'CustomerNo']:
            if col in merged_df.columns:
                customer_col = col
                break
        
        if customer_col:
            # Perform left merge
            merged_df = pd.merge(
                merged_df, 
                df8a_merge[['CUSTOMER_NO', 'ADD_DAYS']], 
                left_on=customer_col, 
                right_on='CUSTOMER_NO', 
                how='left'
            )
            
            # Update ADD Days for INVOICE documents (following line 4885-4889 logic)
            doc_type_col = None
            for col in ['DOCUMENT TYPE', 'Document Type', 'DocumentType', 'DOCUMENT_TYPE']:
                if col in merged_df.columns:
                    doc_type_col = col
                    break
            
            if doc_type_col:
                add_days_raw = merged_df['ADD_DAYS'].astype(str)
                is_888 = add_days_raw.str.startswith('888', na=False)
                # Keep 888xx as string so modal prefix logic can match; use numeric for others
                merged_df['ADD Days'] = np.where(
                    merged_df[doc_type_col] != 'INVOICE',
                    pd.to_numeric(merged_df['ADD Days'], errors='coerce').fillna(0).astype(float),
                    np.where(is_888, merged_df['ADD_DAYS'], pd.to_numeric(merged_df['ADD_DAYS'], errors='coerce').fillna(0).astype(float))
                )
            else:
                add_days_raw = merged_df['ADD_DAYS'].astype(str)
                is_888 = add_days_raw.str.startswith('888', na=False)
                merged_df['ADD Days'] = np.where(is_888, merged_df['ADD_DAYS'], pd.to_numeric(merged_df['ADD_DAYS'], errors='coerce').fillna(pd.to_numeric(merged_df['ADD Days'], errors='coerce').fillna(0)).astype(float))
            
            # Drop the temporary ADD_DAYS column
            merged_df = merged_df.drop(columns=['ADD_DAYS'], errors='ignore')
            merged_df = merged_df.drop(columns=['CUSTOMER_NO'], errors='ignore')
            
            # TASK3: Exempt HOSP000058 and HOSP000526 from df8a_merge in modal only - do not apply ADD Days from SQL
            exempt_customers = ['HOSP000058', 'HOSP000526']
            merged_df.loc[merged_df[customer_col].astype(str).str.strip().isin(exempt_customers), 'ADD Days'] = 0
    
    # Do not apply add_days to rows with negative Balance Due / Remaining Balance / BalanceDue
    bal_col = next((c for c in ['Balance Due', 'Remaining Balance', 'BalanceDue'] if c in merged_df.columns), None)
    if bal_col:
        balance_numeric = pd.to_numeric(merged_df[bal_col], errors='coerce').fillna(0)
        merged_df.loc[balance_numeric < 0, 'ADD Days'] = 0
    
    # Step 2: Overwrite ADD Days from "Currently Listed Customer Names" list (by Name and dept_code)
    if 'display_df_name' in st.session_state and not st.session_state.display_df_name.empty:
        # Ensure Days and dept_code columns exist
        if 'Days' not in st.session_state.display_df_name.columns:
            st.session_state.display_df_name['Days'] = 30
        if 'dept_code' not in st.session_state.display_df_name.columns:
            st.session_state.display_df_name['dept_code'] = ''
        
        # Create a mapping of (Name, dept_code) to Days
        name_dept_days_map = {}
        for _, row in st.session_state.display_df_name.iterrows():
            name = row['Name'].strip() if pd.notna(row['Name']) else None
            dept = str(row.get('dept_code', '')).strip() if pd.notna(row.get('dept_code')) else ''
            days = row.get('Days', 30)
            if name:
                name_dept_days_map[(name, dept)] = days
        
        # Find Name column in merged_df
        name_col = None
        for col in ['Name', 'Customer Name', 'CUSTOMER_NAME', 'CustomerName']:
            if col in merged_df.columns:
                name_col = col
                break
        if name_col and name_dept_days_map:
            # Match by name only (no dept). Normalize: strip, casefold, remove all non-alphanumeric, collapse spaces
            def _norm_name_for_match(s):
                if s is None or (isinstance(s, float) and pd.isna(s)):
                    return ''
                s = str(s).strip().casefold()
                s = re.sub(r'[^a-z0-9\s]', ' ', s)
                return ' '.join(s.split())
            merged_df[name_col] = merged_df[name_col].astype(str).str.strip()
            merged_df['_name_norm'] = merged_df[name_col].apply(_norm_name_for_match)
            
            # TASK3: Exempt HOSP000058 and HOSP000526 - do not apply ADD Days from display_df_name list in modal
            cust_col_exempt = next((c for c in ['Customer No_', 'Customer No', 'CUSTOMER_NO', 'CustomerNo'] if c in merged_df.columns), None)
            exempt_mask = merged_df[cust_col_exempt].astype(str).str.strip().isin(['HOSP000058', 'HOSP000526']) if cust_col_exempt else pd.Series([False] * len(merged_df), index=merged_df.index)
            
            # Update ADD Days for matching Name only; do not overwrite 888xx prefix (age-bucket codes from SQL); skip exempted customers; skip negative balance
            balance_positive = pd.to_numeric(merged_df[bal_col], errors='coerce').fillna(0) >= 0 if bal_col else pd.Series([True] * len(merged_df), index=merged_df.index)
            for (name, dept), days_value in name_dept_days_map.items():
                name_norm = _norm_name_for_match(name)
                mask = merged_df['_name_norm'] == name_norm
                skip_888 = merged_df['ADD Days'].astype(str).str.startswith('888', na=False)
                if mask.any():
                    merged_df.loc[mask & ~skip_888 & ~exempt_mask & balance_positive, 'ADD Days'] = float(days_value)
            
            merged_df.drop(columns=['_name_norm'], inplace=True, errors='ignore')
    
    # Step 3: Adjust Due Date by ADD Days and recompute aging bucket columns for the modal display
    if not merged_df.empty and 'ADD Days' in merged_df.columns and 'Due Date' in merged_df.columns:
        # Exclude grand total row if present (last row is often a totals row)
        data_only = merged_df.iloc[:-1].copy() if len(merged_df) > 1 else merged_df.copy()
        grand_total_row = merged_df.iloc[-1:].copy() if len(merged_df) > 1 else None
        
        data_only['Due Date'] = pd.to_datetime(data_only['Due Date'], errors='coerce')
        # Keep ADD Days as string when it starts with 888 so prefix (age-bucket) logic can match; convert others to int
        add_days_str = data_only['ADD Days'].astype(str)
        is_888_prefix = add_days_str.str.startswith('888', na=False)
        data_only.loc[~is_888_prefix, 'ADD Days'] = pd.to_numeric(data_only.loc[~is_888_prefix, 'ADD Days'], errors='coerce').fillna(0).astype(int)
        if 'Original Due Date' in data_only.columns:
            data_only['Original Due Date'] = pd.to_datetime(data_only['Original Due Date'], errors='coerce')
        if 'AsOfDate' in data_only.columns:
            _asof = data_only['AsOfDate'].dropna()
            ref_date = pd.to_datetime(_asof.iloc[0], errors='coerce') if not _asof.empty else None
        else:
            ref_date = None
        if 'Posting Date' in data_only.columns:
            data_only['Posting Date'] = pd.to_datetime(data_only['Posting Date'], errors='coerce')
        
        # Payment terms and customer exceptions (same as update_calculations)
        pay_col = 'Payment_Terms' if 'Payment_Terms' in data_only.columns else ('Payment Terms Code' if 'Payment Terms Code' in data_only.columns else None)
        if pay_col:
            data_only['_pay_numeric'] = data_only[pay_col].astype(str).str.extract(r'(\d+)', expand=False).astype(float).fillna(0).astype(int)
        else:
            data_only['_pay_numeric'] = 0
        cust_col = 'Customer No_' if 'Customer No_' in data_only.columns else None
        skip_cust = set(['HOSP000058', 'HOSP000526', 'CORP000323']) if cust_col else set()
        
        def modal_due_date(row):
            # Do not apply add_days if Balance Due / Remaining Balance / BalanceDue is negative
            for _bal_col in ['Balance Due', 'Remaining Balance', 'BalanceDue']:
                if _bal_col in row.index:
                    try:
                        bal = pd.to_numeric(row[_bal_col], errors='coerce')
                        if pd.notna(bal) and bal < 0:
                            return row['Due Date']
                    except (ValueError, TypeError):
                        pass
                    break
            add_val = row['ADD Days']
            if pd.isna(add_val) or str(add_val).startswith('888'):
                add_d = 0  # 888xx codes are handled by prefix block; do not add days here
            else:
                add_d = int(float(add_val)) if pd.notna(add_val) else 0
            if add_d == 0:
                return row['Due Date']
            if pd.isna(row['Due Date']):
                return row['Due Date']
            pay_term = int(row['_pay_numeric']) if pd.notna(row['_pay_numeric']) else 0
            if pay_term > 45:
                return row['Due Date']
            if cust_col and row.get(cust_col) in skip_cust:
                return row['Due Date']
            base = row['Original Due Date'] if 'Original Due Date' in data_only.columns and pd.notna(row.get('Original Due Date')) else row['Due Date']
            if pd.isna(base):
                return row['Due Date']
            return base + timedelta(days=add_d)
        
        data_only['Due Date'] = data_only.apply(modal_due_date, axis=1)
        # Reset ADD Days to 0 where we did not adjust (payment terms > 45), except for "Currently Listed Customer Names" so their add days stay visible
        add_days_numeric = pd.to_numeric(data_only['ADD Days'], errors='coerce').fillna(0).astype(int)
        no_adj = (data_only['_pay_numeric'] > 45) & (add_days_numeric > 0)
        if cust_col:
            no_adj = no_adj & (~data_only[cust_col].isin(skip_cust))
        # Do not zero ADD Days for names in display_df_name list (Actimed, Planet, etc.) so they still show their requested add days
        _name_col = None
        for c in ['Name', 'Customer Name', 'CUSTOMER_NAME', 'CustomerName']:
            if c in data_only.columns:
                _name_col = c
                break
        if 'display_df_name' in st.session_state and not st.session_state.display_df_name.empty and _name_col:
            def _norm_n(s):
                if s is None or (isinstance(s, float) and pd.isna(s)):
                    return ''
                s = str(s).strip().casefold()
                s = re.sub(r'[^a-z0-9\s]', ' ', s)
                return ' '.join(s.split())
            listed_names = {_norm_n(row['Name']) for _, row in st.session_state.display_df_name.iterrows() if pd.notna(row.get('Name')) and str(row['Name']).strip()}
            data_only['_name_norm'] = data_only[_name_col].astype(str).apply(_norm_n)
            no_adj = no_adj & (~data_only['_name_norm'].isin(listed_names))
            data_only.drop(columns=['_name_norm'], inplace=True, errors='ignore')
        data_only.loc[no_adj, 'ADD Days'] = 0
        data_only.drop(columns=['_pay_numeric'], inplace=True, errors='ignore')
        
        # Task1–Task3: Maintain age bucket by ADD Days prefix (88801/88831/88861/88891 from SQL); compute ADD Days applied; adjust Due Date
        if ref_date is not None and pd.notna(ref_date):
            if 'Remarks' not in data_only.columns:
                data_only['Remarks'] = ''
            ref_date = pd.Timestamp(ref_date)
            configs = [
                {'prefix': '88801', 'target_days': 1, 'remark': 'Maintain to 1 - 30 days'},
                {'prefix': '88831', 'target_days': 31, 'remark': 'Maintain to 31 - 60 days'},
                {'prefix': '88861', 'target_days': 61, 'remark': 'Maintain to 61 - 90 days'},
                {'prefix': '88891', 'target_days': 91, 'remark': 'Maintain to 91+ days'}
            ]
            bal_col_do = next((c for c in ['Balance Due', 'Remaining Balance', 'BalanceDue'] if c in data_only.columns), None)
            balance_positive_do = pd.to_numeric(data_only[bal_col_do], errors='coerce').fillna(0) >= 0 if bal_col_do else pd.Series([True] * len(data_only), index=data_only.index)
            for config in configs:
                mask = data_only['ADD Days'].astype(str).str.startswith(config['prefix'], na=False) & balance_positive_do
                if not mask.any():
                    continue
                target_due = ref_date - pd.Timedelta(days=config['target_days'])
                if 'Original Due Date' in data_only.columns:
                    orig_due = data_only.loc[mask, 'Original Due Date']
                else:
                    orig_due = data_only.loc[mask, 'Due Date'] - pd.to_timedelta(pd.to_numeric(data_only.loc[mask, 'ADD Days'], errors='coerce').fillna(0).astype(int), unit='D')
                add_days_applied = (target_due - orig_due).dt.days.fillna(0).astype(int)
                data_only.loc[mask, 'Due Date'] = target_due
                data_only.loc[mask, 'ADD Days'] = add_days_applied.values
                data_only.loc[mask, 'Remarks'] = data_only.loc[mask, 'Remarks'].fillna('') + " | " + config['remark']
        
        # Recompute aging and bucket columns (ensure ref_date and Due Date are datetime to avoid str vs Timestamp TypeError)
        if ref_date is not None and pd.notna(ref_date):
            ref_date = pd.Timestamp(ref_date)
            def _aging_days(row):
                due = pd.to_datetime(row['Due Date'], errors='coerce')
                return (ref_date - due).days if pd.notna(due) else None
            data_only['AgingDays'] = data_only.apply(_aging_days, axis=1)
            bal_col = None
            for c in ['Balance Due', 'Remaining Balance', 'BalanceDue']:
                if c in data_only.columns:
                    bal_col = c
                    break
            if bal_col:
                data_only['Current'] = data_only.apply(
                    lambda row: row[bal_col] if row['AgingDays'] is not None and row['AgingDays'] < 1 else None, axis=1
                )
                data_only['Days_1_to_30'] = data_only.apply(
                    lambda row: row[bal_col] if row['AgingDays'] is not None and 1 <= row['AgingDays'] <= 30 else None, axis=1
                )
                data_only['Days_31_to_60'] = data_only.apply(
                    lambda row: row[bal_col] if row['AgingDays'] is not None and 31 <= row['AgingDays'] <= 60 else None, axis=1
                )
                data_only['Days_61_to_90'] = data_only.apply(
                    lambda row: row[bal_col] if row['AgingDays'] is not None and 61 <= row['AgingDays'] <= 90 else None, axis=1
                )
                data_only['Over_91_Days'] = data_only.apply(
                    lambda row: row[bal_col] if row['AgingDays'] is not None and row['AgingDays'] >= 91 else None, axis=1
                )
                data_only['Total Target'] = data_only.apply(
                    lambda row: row[bal_col] if row['AgingDays'] is not None and row['AgingDays'] > 0 else 0, axis=1
                )
        
        merged_df = pd.concat([data_only, grand_total_row], ignore_index=True) if grand_total_row is not None and len(grand_total_row) > 0 else data_only
    
    # Display the merged dataframe (convert date columns to string for PyArrow compatibility)
    st.header("A/R with Add Days merge with SQL")
    display_merged = merged_df.copy()
    date_columns = ['Posting Date', 'Due Date', 'AsOfDate', 'Original Due Date']
    for col in date_columns:
        if col not in display_merged.columns:
            continue
        if pd.api.types.is_datetime64_any_dtype(display_merged[col]):
            display_merged[col] = display_merged[col].dt.strftime('%Y-%m-%d')
        elif display_merged[col].dtype == 'object':
            try:
                # Handle pandas datetime and Python datetime.date / datetime.datetime
                display_merged[col] = pd.to_datetime(display_merged[col], errors='coerce').dt.strftime('%Y-%m-%d')
            except (ValueError, TypeError, AttributeError):
                # Fallback: convert date/datetime objects to string by element
                def _date_to_str(x):
                    if x is None or pd.isna(x):
                        return ''
                    if hasattr(x, 'strftime'):
                        return x.strftime('%Y-%m-%d')
                    return str(x)
                display_merged[col] = display_merged[col].apply(_date_to_str)
    col_cfg = _numeric_column_config(display_merged)
    st.dataframe(
        display_merged,
        use_container_width=True,
        hide_index=True,
        column_config=col_cfg
    )
    
    # Add download buttons for the merged data
    if 'date_to_str' in st.session_state:
        csv_data = merged_df.to_csv(index=False).encode('utf-8')
        dl_col1, dl_col2 = st.columns(2)
        with dl_col1:
            st.download_button(
                label="Download CSV",
                data=csv_data,
                file_name=f"AR_with_Add_Days_{st.session_state.date_to_str}.csv",
                mime="text/csv",
                key="ar_add_days_dl_csv"
            )
        with dl_col2:
            excel_df = _prepare_ar_excel_df(merged_df)
            try:
                output = BytesIO()
                try:
                    excel_df.to_excel(output, index=False, engine='openpyxl')
                except ImportError:
                    excel_df.to_excel(output, index=False, engine='xlsxwriter')
                excel_data = output.getvalue()
                st.download_button(
                    label="📗 Download Excel",
                    data=excel_data,
                    file_name=f"AR_with_Add_Days_{st.session_state.date_to_str}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="ar_add_days_dl_excel"
                )
            except (ImportError, ModuleNotFoundError):
                st.warning("Install 'openpyxl' or 'xlsxwriter' for Excel download.")
    
@st.fragment
def show_details_df_fragment():
    result_df_co = st.session_state.result_df_co.copy()
    result_df_cur = st.session_state.result_df_cur.copy()   
    result_df_cod = st.session_state.result_df_cod.copy()
    show_details = st.checkbox("Show OVERDUE, CURRENT, COD Details", value=False, key="show_occ_details")
    if show_details:
        st.subheader("Target base w/ category - OVERDUE Details")                            
        dataframe_fragement(result_df_co)
        st.subheader("Target base w/ category - CURRENT Details")
        dataframe_fragement(result_df_cur)  
        st.subheader("Target base w/ category - COD Details")                   
        dataframe_fragement(result_df_cod)    

@st.fragment
def target_category_group_fragment(overdue_df, current_df, cod_df):
    # Merge the grouped DataFrames on the common keys (outer join to include all unique groups)
    merged_df = overdue_df.merge(current_df, on=['Target_Category_Name', 'DSS2_Name', 'SCR_NAME'], how='outer')
    merged_df = merged_df.merge(cod_df, on=['Target_Category_Name', 'DSS2_Name', 'SCR_NAME'], how='outer')

    # Fill any NaN values with 0 (for groups missing in one or more DataFrames)
    merged_df['Overdue_Amount'] = merged_df['Overdue_Amount'].fillna(0)
    merged_df['Current_Amount'] = merged_df['Current_Amount'].fillna(0)
    merged_df['COD_Amount'] = merged_df['COD_Amount'].fillna(0)
    merged_df['TOTAL_TARGET'] = abs(merged_df['Overdue_Amount']) + abs(merged_df['Current_Amount']) + abs(merged_df['COD_Amount'])

    col_cfg = _numeric_column_config(merged_df)
    st.dataframe(merged_df, use_container_width=True, hide_index=True, key=f'target_{time.time()}', column_config=col_cfg) 
    left_col, right_col = st.columns([3, 1])  # Adjust ratios: smaller left pushes buttons further right    
    with left_col: 
        csv = merged_df.to_csv(index=False)
        st.download_button(
            label="Download Report as CSV",
            data=csv,
            file_name="Target Base on Category_Summary.csv",
            mime="text/csv")
    with right_col:
        # Sub-columns for the two config buttons: equal width to keep them close and adjacent
        btn_co_col, btn_cod_col = st.columns([1, 1])
            
        with btn_co_col:
            if st.button("DUE and CUR Conditions", key="btn_co"):                            
                config_modal_fragment("CO")  # This triggers the modal

        with btn_cod_col:
            if st.button("COD Conditions", key="btn_cod"):                    
                config_modal_fragment("COD")     
        
@st.fragment
def dataframe_fragement(result_df):
    # Dataframe with new columns
    col_cfg = _numeric_column_config(result_df)
    st.dataframe(result_df, use_container_width=True, hide_index=True, key=f'target_{time.time()}', column_config=col_cfg) 
    csv = result_df.to_csv(index=False)
    st.download_button(
        label="Download Report as CSV",
        data=csv,
        file_name=f"Target Base on Category_Details{time.time()}.csv",
        mime="text/csv") 

@st.fragment
def target_category_fragment(main_df, second_df, target):
    ##########################################################################################################
    # Complex matching rules | Multiple matching rules with exclusions
    #### First Table : Main Table (Raw Data)
    #### Second Table : Target Category Conditions (Table with conditions to match)   
    ##########################################################################################################
    # USE OF import re  # For simple parsing of conditions like "< 0"
    # SAMPLE DATA
    # main_df = pd.read_csv('MainTable.csv') # session state of dataframe (OVERDUE, CURRENT, COD)
    # second_df = pd.read_csv('CO_Conditions.csv') # Target Category Conditions (OVERDUE AND CURRENT, COD) from CSV File

    # Clean column names if needed (e.g., strip spaces)
    main_df.columns = main_df.columns.str.strip()
    second_df.columns = second_df.columns.str.strip()

    # Define a function to check if a main_row matches a second_row's conditions
    def matches_conditions(main_row, second_row):
        # NEW FIX: Skip if ALL condition columns are N/A 
        condition_cols = ['Payment_Terms', 'Not_Payment_Terms', 'DEPT CODE', 'Not_DEPT CODE', 'Name','Not_Name', 'Balance Due', 'SR2', 'DSS_NAME', 'SCR_NAME', 'city_name']
        all_na = all(pd.isna(second_row.get(col, 'N/A')) or second_row.get(col, 'N/A') == 'N/A' for col in condition_cols)
        if all_na:
            return False  # Ignore this rule entirely
        
        # Helper to check exact or contains match
        def col_match(main_val, second_val, main_row_ref=None):
            if pd.isna(second_val) or second_val == 'N/A':
                return True # <-- Exits HERE, ignoring this condition because of 'N/A' in second table, move to next conditions. 
            
            second_val_str = str(second_val)
            # Check if second_val contains curly brackets
            has_curly_brackets = '{' in second_val_str and '}' in second_val_str
            
            # If main_val is null/empty, only proceed if second_val has curly brackets
            if pd.isna(main_val) or (isinstance(main_val, str) and main_val.strip() == ''):
                if not has_curly_brackets:
                    return False # <-- Exits HERE, fails to meet the matching rule if no curly brackets, move to the next row of Main Table
                # If has curly brackets, proceed to check referenced column (main_str not needed)
            
            def token_matches(token):
                """Handle normal tokens and {COLUMN} value tokens."""
                token = token.strip()
                # Column reference pattern: {COL_NAME} expected_value
                if token.startswith('{') and '}' in token and main_row_ref is not None:
                    col_name = token[1:token.index('}')].strip()
                    expected = token[token.index('}') + 1 :].strip()
                    if expected == '':
                        return False
                    actual = main_row_ref.get(col_name, '')
                    # Check if the referenced column value is null/empty
                    if pd.isna(actual) or (isinstance(actual, str) and str(actual).strip() == ''):
                        return False # <-- Referenced column is null/empty, fails
                    return str(actual).strip().upper() == expected.upper()
                # Normal text match - only if main_val is not null/empty
                if pd.isna(main_val) or (isinstance(main_val, str) and main_val.strip() == ''):
                    return False # Can't match normal text if main_val is null/empty
                main_str = str(main_val).strip().upper()
                return token.upper() in main_str or main_str == token.upper()

            if '|' in second_val_str:
                options = [opt for opt in second_val_str.split('|')]
                return any(token_matches(opt) for opt in options)
            else:
                return token_matches(second_val_str)
        
        # Helper for exclusion (Not_ columns)
        def not_col_match(main_val, not_second_val):
            if pd.isna(not_second_val) or not_second_val == 'N/A':
                return True # <-- Exits HERE, ignoring this condition because of 'N/A' in second table, move to next conditions. 
            if pd.isna(main_val):
                return True # <-- Exits HERE, TRUE means proceed to the next conditions. 
            
            # Return TRUE/FALSE based on matching condition.
            main_str = str(main_val).strip().upper()
            if '|' in str(not_second_val):
                options = [opt.strip().upper() for opt in str(not_second_val).split('|')]
                return not any(opt in main_str for opt in options)
            else:
                not_str = str(not_second_val).strip().upper()
                return not_str not in main_str
        
        # Helper for Balance Due numeric conditions
        def balance_match(main_val, second_val):
            if pd.isna(second_val) or second_val == 'N/A':
                return True # <-- Exits HERE, ignoring this condition because of 'N/A' in second table, move to next conditions. RETURN without carrying a return value
            if pd.isna(main_val):
                return False # <-- Exits HERE, fails to meet the matching rule, move to the next row of Main Table (@ for loop in assign_categories)
            try:
                bal = float(main_val)
                cond = str(second_val).strip()
                if cond == '< 0':
                    return bal < 0  # RETURN TRUE if condition met, if bal less than 0 (NEGATIVE VALUE)
                elif cond == '> 0':
                    return bal > 0 # RETURN TRUE if condition met, if bal greater than 0 (POSITIVE VALUE)
                else:
                    return True  # Add more conditions as needed
            except ValueError:
                return False
                   
        if target in ['CO', 'CUR']:    
            if not col_match(main_row.get('Payment_Terms', ''), second_row.get('Payment_Terms', 'N/A'), main_row):
                return False
            if not not_col_match(main_row.get('Payment_Terms', ''), second_row.get('Not_Payment_Terms', 'N/A')):
                return False
            if not col_match(main_row.get('DEPT CODE', ''), second_row.get('DEPT CODE', 'N/A'), main_row):
                return False
            if not not_col_match(main_row.get('DEPT CODE', ''), second_row.get('Not_DEPT CODE', 'N/A')):
                return False
            if not col_match(main_row.get('Name', ''), second_row.get('Name', 'N/A'), main_row):
                return False
            if not not_col_match(main_row.get('Name', ''), second_row.get('Not_Name', 'N/A')):
                return False 
            # # #     
            if not balance_match(main_row.get('Balance Due', 0), second_row.get('Balance Due', 'N/A')):
                return False
            if not balance_match(main_row.get('City', 0), second_row.get('city_name', 'N/A')):
                return False
            
            # REVISED FIX: Handle DSS_NAME with '0 | Head Office' (normal match + wildcard for '0' if blanks/empty)
            dss_second = second_row.get('DSS_NAME', 'N/A')
            dss_main = main_row.get('DSS_NAME', '')
            
            is_blank_main = pd.isna(dss_main) or str(dss_main).strip() == ''
            # is_blank_second = pd.isna(dss_second) or str(dss_second).strip() == ''
            # if is_blank_second and not is_blank_main:
            #     return False
            
            if '|' in str(dss_second):
                options = [opt.strip().upper() for opt in str(dss_second).split('|')]
                main_str = str(dss_main).strip().upper()
                has_blank_wildcard = any(opt in ('', '0') for opt in options)
                non_blank_opts = [opt for opt in options if opt not in ('', '0')]
                if has_blank_wildcard:
                    if not is_blank_main and not any(opt in main_str for opt in non_blank_opts):
                        return False
                else:
                    if not any(opt in main_str for opt in non_blank_opts):
                        return False                   
            else: 
                # Single value: normal match
                if not col_match(dss_main, dss_second):
                    return False                
        
        if target == 'CO':
            # Check all conditions 
            # using 'not' is to catch the FALSE or not match, and then it will trigger this condition and RETURN FALSE
            # if TRUE, it will proceed to the next condition
            
            if not col_match(main_row.get('SR2', ''), second_row.get('SR2', 'N/A'), main_row): 
                return False
            if not not_col_match(main_row.get('SR2', ''), second_row.get('Not_SR2', 'N/A')): 
                return False            
            # # #
                    
        elif target == 'CUR':
            # Check all conditions 
            # using 'not' is to catch the FALSE or not match, and then it will trigger this condition and RETURN FALSE
            # if TRUE, it will proceed to the next condition
            
            if not col_match(main_row.get('SCR_NAME', ''), second_row.get('SR2', 'N/A'), main_row): 
                return False
            if not not_col_match(main_row.get('SCR_NAME', ''), second_row.get('Not_SR2', 'N/A')): 
                return False            
            # # #
                    
        elif target == 'COD':
            if not col_match(main_row.get('SCR_NAME', ''), second_row.get('SR2', 'N/A'), main_row): 
                return False
            
        return True

    # Perform the conditional left join
    def assign_categories(main_row):
        for _, second_row in second_df.iterrows():
            if matches_conditions(main_row, second_row):
                return pd.Series({
                    'Target_Category_Name': second_row['Target_Category_Name'],
                    'DSS2_Name': second_row['DSS2_Name']
                })
        return pd.Series({'Target_Category_Name': np.nan, 'DSS2_Name': np.nan})

    # Apply the function (this may take time for large MainTable; optimize with vectorization if needed)
    result_df = main_df.copy()
    category_matches = result_df.apply(assign_categories, axis=1)
    # Drop existing Target_Category_Name and DSS2_Name columns if they exist to avoid MultiIndex issues
    result_df = result_df.drop(columns=['Target_Category_Name', 'DSS2_Name'], errors='ignore')
    result_df = pd.concat([result_df, category_matches], axis=1)    
    return result_df  # Return Dataframe with new columns

def apply_category_to_display_df(display_df):
    """
    Apply category conditions from CO_Conditions.csv to display_df6_view.
    Adds DSS2_Name and Category columns, and updates SR2 based on conditions.
    Forces SR2 to 'Head Office' when Balance Due < 0.
    """
    if display_df.empty:
        return display_df
    
    # Load conditions CSV
    try:
        second_df = pd.read_csv('CO_Conditions.csv')
    except FileNotFoundError:
        # If file not found, return original dataframe with empty category columns
        display_df['DSS2_Name'] = ''
        display_df['Category'] = ''
        return display_df
    
    # Clean column names
    display_df.columns = display_df.columns.str.strip()
    second_df.columns = second_df.columns.str.strip()

    # Create expected columns when only aliases exist (e.g., SCR_NAME/SCR)
    alias_map = {
        'SR2': ['SCR_NAME', 'SR_NAME'],
        'SR_Code2': ['SCR', 'SR_CODE2', 'SR_CODE'],
        'Balance Due': ['Remaining Balance', 'BalanceDue'],
        'Payment_Terms': ['PaymentTermsCode', 'Payment Terms Code'],
        'Name': ['CustomerName', 'Customer Name'],
        'DSS_NAME': ['DSS_NAME']
    }
    for target_col, aliases in alias_map.items():
        if target_col not in display_df.columns:
            for alias in aliases:
                if alias in display_df.columns:
                    display_df[target_col] = display_df[alias]
                    break
            else:
                display_df[target_col] = '' if target_col != 'Balance Due' else 0
    
    # Helper functions (same as in target_category_fragment)
    def col_match(main_val, second_val):
        if pd.isna(second_val) or second_val == 'N/A':
            return True
        if pd.isna(main_val):
            return False
        main_str = str(main_val).strip().upper().replace('<','')
        if '|' in str(second_val):
            options = [opt.strip().upper().replace('<','') for opt in str(second_val).split('|')]
            return any(opt in main_str for opt in options)
        else:
            second_str = str(second_val).strip().upper().replace('<','')
            return second_str in main_str or main_str == second_str
    
    def not_col_match(main_val, not_second_val):
        if pd.isna(not_second_val) or not_second_val == 'N/A':
            return True
        if pd.isna(main_val): 
            return True
        main_str = str(main_val).strip().upper().replace('<','')
        if '|' in str(not_second_val):
            options = [opt.strip().upper().replace('<','') for opt in str(not_second_val).split('|')]
            return not any(opt in main_str for opt in options)
        else:
            not_str = str(not_second_val).strip().upper().replace('<','')
            return not_str not in main_str
    
    def balance_match(main_val, second_val):
        if pd.isna(second_val) or second_val == 'N/A':
            return True
        if pd.isna(main_val):
            return False
        try:
            bal = float(main_val)
            cond = str(second_val).strip()
            if cond == '< 0':
                return bal < 0
            elif cond == '> 0':
                return bal > 0
            else:
                return True
        except ValueError:
            return False
    
    def matches_conditions(main_row, second_row, check_sr2=True):
        # Skip if ALL condition columns are N/A
        condition_cols = ['Payment_Terms', 'Not_Payment_Terms', 'DEPT CODE', 'Not_DEPT CODE', 'Name', 'Not_Name', 'Balance Due', 'SR2', 'DSS_NAME', 'city_name']
        all_na = all(pd.isna(second_row.get(col, 'N/A')) or second_row.get(col, 'N/A') == 'N/A' for col in condition_cols)
        if all_na:
            return False
        
        # Check conditions (same as target_category_fragment for 'CO' target)
        if not col_match(main_row.get('Payment_Terms', ''), second_row.get('Payment_Terms', 'N/A')):
            return False
        if not not_col_match(main_row.get('Payment_Terms', ''), second_row.get('Not_Payment_Terms', 'N/A')):
            return False
        if not col_match(main_row.get('DEPT CODE', ''), second_row.get('DEPT CODE', 'N/A')):
            return False
        if not not_col_match(main_row.get('DEPT CODE', ''), second_row.get('Not_DEPT CODE', 'N/A')):
            return False
        if not col_match(main_row.get('Name', ''), second_row.get('Name', 'N/A')):
            return False
        if not not_col_match(main_row.get('Name', ''), second_row.get('Not_Name', 'N/A')):
            return False
        if not balance_match(main_row.get('Balance Due', 0), second_row.get('Balance Due', 'N/A')):
            return False
        if not col_match(main_row.get('SR2', ''), second_row.get('SR2', 'N/A')):
            return False
        if not not_col_match(main_row.get('SR2', ''), second_row.get('Not_SR2', 'N/A')):
            return False
        if not col_match(main_row.get('City', ''), second_row.get('city_name', 'N/A')):
            return False        
        # Handle DSS_NAME matching
        dss_second = second_row.get('DSS_NAME', 'N/A')
        dss_main = main_row.get('DSS_NAME', '')
        is_blank_main = pd.isna(dss_main) or str(dss_main).strip() == ''
        
        if '|' in str(dss_second):
            options = [opt.strip().upper() for opt in str(dss_second).split('|')]
            main_str = str(dss_main).strip().upper()
            has_blank_wildcard = any(opt in ('', '0') for opt in options)
            non_blank_opts = [opt for opt in options if opt not in ('', '0')]
            if has_blank_wildcard:
                if not is_blank_main and not any(opt in main_str for opt in non_blank_opts):
                    return False
            else:
                if not any(opt in main_str for opt in non_blank_opts):
                    return False
        else:
            if not col_match(dss_main, dss_second):
                return False
        
        # Check SR2 conditions only if check_sr2 is True
        # For updating SR2, we skip this check to allow SR2 to be updated
        if check_sr2:
            if not col_match(main_row.get('SR2', ''), second_row.get('SR2', 'N/A')):
                return False
            if not not_col_match(main_row.get('SR2', ''), second_row.get('Not_SR2', 'N/A')):
                return False
        
        return True
    
    # Assign categories and update SR2
    def assign_categories_and_sr2(main_row):
        balance_due = main_row.get('Balance Due', 0)
        try:
            balance_numeric = float(balance_due)
        except (ValueError, TypeError):
            balance_numeric = 0
        
        matched_row = None

        # Define columns that make a rule "specific/aware" when provided
        aware_cols = [
            'DSS_NAME', 'SR2', 'Not_SR2', 'Payment_Terms', 'Not_Payment_Terms',
            'DEPT CODE', 'Not_DEPT CODE', 'Name', 'Not_Name', 'Balance Due', 'city_name'
        ]

        def is_specific_rule(row):
            for col in aware_cols:
                val = row.get(col, 'N/A')
                if pd.notna(val):
                    sval = str(val).strip()
                    if sval not in ('', 'N/A'):
                        return True
            return False

        # Pass 1: try to match specific/aware rules first, with SR2 checking
        for _, second_row in second_df.iterrows():
            if is_specific_rule(second_row):
                if matches_conditions(main_row, second_row, check_sr2=True):
                    matched_row = second_row
                    break

        # Pass 2: if no specific match, allow broader match without SR2 check (backward compatibility)
        if matched_row is None:
            for _, second_row in second_df.iterrows():
                if matches_conditions(main_row, second_row, check_sr2=False):
                    matched_row = second_row
                    break
        
        if matched_row is not None:
            # Condition matched - assign category and DSS2_Name
            category_name = matched_row['Target_Category_Name']
            dss2_name = matched_row['DSS2_Name']
            
            # Determine SR2 value
            if balance_numeric < 0:
                # Force SR2 to Head Office only for negative balances
                sr2_value = 'Head Office'
            else:
                # Keep original SR2 (we are not applying SR2 from conditions here)
                sr2_value = main_row.get('SR2', '')
            
            return pd.Series({
                'Target_Category_Name': category_name,
                'DSS2_Name': dss2_name,
                'SR2': sr2_value
            })
        
        # No match found - keep original values
        return pd.Series({
            'Target_Category_Name': np.nan,
            'DSS2_Name': np.nan,
            'SR2': main_row.get('SR2', '')  # Keep original SR2 if no match
        })
    
    # Apply the function
    result_df = display_df.copy()

    # Pre-classify Head Office negatives before category matching to avoid overwrites.
    balance_due_numeric = pd.to_numeric(result_df['Balance Due'], errors='coerce')
    ho_negative_mask = (balance_due_numeric < 0) & (result_df['SR2'].astype(str).str.strip() == 'Head Office')
    if 'Category' not in result_df.columns:
        result_df['Category'] = ''
    if 'DSS2_Name' not in result_df.columns:
        result_df['DSS2_Name'] = ''
    result_df.loc[ho_negative_mask, 'Category'] = '5 Head Office - Reconciling Items'
    result_df.loc[ho_negative_mask, 'DSS2_Name'] = '5.1 Head office'

    # Store original SR2 values before category matching (for special conditions check)
    original_sr2 = result_df['SR2'].copy() if 'SR2' in result_df.columns else pd.Series()
    category_matches = result_df.apply(assign_categories_and_sr2, axis=1)
    
    # Add new columns
    result_df['DSS2_Name'] = category_matches['DSS2_Name']
    result_df['Category'] = category_matches['Target_Category_Name']
    
    # Update SR2 column based on matches
    # For rows where a match was found, update SR2 (already handled in assign_categories_and_sr2)
    sr2_mask = ~category_matches['SR2'].isna()
    result_df.loc[sr2_mask, 'SR2'] = category_matches.loc[sr2_mask, 'SR2']

    # Re-apply Head Office negative-balance classification after matching to ensure
    # it is not overwritten by category assignment.
    balance_due_numeric = pd.to_numeric(result_df['Balance Due'], errors='coerce')
    ho_negative_mask = (balance_due_numeric < 0) & (result_df['SR2'].astype(str).str.strip() == 'Head Office')
    if 'Category' not in result_df.columns:
        result_df['Category'] = ''
    if 'DSS2_Name' not in result_df.columns:
        result_df['DSS2_Name'] = ''
    result_df.loc[ho_negative_mask, 'Category'] = '5 Head Office - Reconciling Items'
    result_df.loc[ho_negative_mask, 'DSS2_Name'] = '5.1 Head office'
    
    # Apply special conditions for specific SR2 values (check original SR2, not updated)
    result_df = apply_special_sr2_conditions(result_df, original_sr2)
    
    # Head Office must always have SR_Code2=ZZZ (when we force SR2 to Head Office, SR_Code2 was not set)
    sr_code2_col = 'SR_CODE2' if 'SR_CODE2' in result_df.columns else ('SR_Code2' if 'SR_Code2' in result_df.columns else None)
    if sr_code2_col:
        ho_mask = result_df['SR2'].astype(str).str.strip() == 'Head Office'
        result_df.loc[ho_mask, sr_code2_col] = 'ZZZ'
    
    return result_df

def apply_special_sr2_conditions(df, original_sr2=None):
    """
    Apply special conditions for specific SR2 values:
    - If ORIGINAL SR2 equals "Lorenzo Mejia" or "Ronald Torrecampo":
      - Force SR2 to "Head Office"
      - Force Category to "5 Head Office - Reconciling Items"
      - If Balance Due < 0: DSS2_Name = "5.1 Head office"
      - If Balance Due > 0: DSS2_Name = "5.2 Head office - Accts. Rec."
    
    Args:
        df: DataFrame to update
        original_sr2: Series with original SR2 values (before category matching)
    """
    if df.empty:
        return df
    
    # Ensure required columns exist
    if 'SR2' not in df.columns:
        return df
    
    # Define special SR2 names
    special_sr2_names = ["Lorenzo Mejia", "Ronald Torrecampo"]
    
    # Use original SR2 if provided, otherwise use current SR2
    if original_sr2 is not None and len(original_sr2) == len(df):
        sr2_to_check = original_sr2
    else:
        sr2_to_check = df['SR2']
    
    # Create mask for rows with special SR2 values (check original SR2)
    special_mask = sr2_to_check.astype(str).str.strip().isin(special_sr2_names)
    
    if not special_mask.any():
        return df  # No rows match, return as is
    
    # Ensure Balance Due column exists and is numeric
    if 'Balance Due' not in df.columns:
        return df
    
    # Convert Balance Due to numeric
    balance_due_numeric = pd.to_numeric(df['Balance Due'], errors='coerce')
    
    # Apply special conditions
    # 1. Force SR2 to "Head Office"
    df.loc[special_mask, 'SR2'] = 'Head Office'
    
    # 2. Force Category to "5 Head Office - Reconciling Items"
    if 'Category' not in df.columns:
        df['Category'] = ''
    df.loc[special_mask, 'Category'] = '5 Head Office - Reconciling Items'
    
    # 3. Set DSS2_Name based on Balance Due
    if 'DSS2_Name' not in df.columns:
        df['DSS2_Name'] = ''
    
    # For Balance Due < 0: DSS2_Name = "5.1 Head office"
    negative_balance_mask = special_mask & (balance_due_numeric < 0)
    df.loc[negative_balance_mask, 'DSS2_Name'] = '5.1 Head office'
    
    # For Balance Due > 0: DSS2_Name = "5.2 Head office - Accts. Rec."
    positive_balance_mask = special_mask & (balance_due_numeric > 0)
    df.loc[positive_balance_mask, 'DSS2_Name'] = '5.2 Head office - Accts. Rec.'
    
    # For Balance Due == 0: Keep existing DSS2_Name or set to empty
    # (No specific requirement for zero balance)
    
    
    return df

@st.fragment
def date_range_fragment():
    ###
    # Use columns to place logo on the left and logout button on the right
    col1, col2, col3 = st.columns([13, 13, 5])  # Adjust proportions for spacing
    with col1:
        # pass
        st.image("InnoGen-Pharmaceuticals-Inc_Logo.png", width=200)
    with col3:
        if st.button("Logout"):
            print("Logout button clicked")
            st.session_state.authenticated = False
            st.session_state.username = None
            st.session_state.access_level = None
            print("Logout successful, forcing rerun")
            st.rerun()
    
        # Display welcome message with username and access level
        print(f"Displaying welcome message for {st.session_state.username}")
        st.markdown(f"""
            <p style="color: #6b3fa0;"> Welcome, {st.session_state.username}<br>Access Level: {st.session_state.access_level}</p>
        """, unsafe_allow_html=True)


    # Existing app content
    # st.image("InnoGen-Pharmaceuticals-Inc_Logo.png", width=200)
    st.markdown("""
        <h1 style="color: #6b3fa0; font-weight: bold; text-align: center;">
            <b> A/R as of the month </b>
        </h1>
        <!-- Subtitle Below the title -->
 
        <h3 style="color: #6b3fa0; text-align: center;">
            with Collection Target Performance Report
        </h3>     
    """, unsafe_allow_html=True)

    def get_last_day_of_month(input_date):
        first_next_month = input_date.replace(day=1) + relativedelta(months=1)
        return first_next_month - timedelta(days=1)

    # Initialize session_state if needed
    if 'date_from' not in st.session_state:
        st.session_state.date_from = date.today().replace(day=1)
    if 'date_to' not in st.session_state:
        st.session_state.date_to = get_last_day_of_month(st.session_state.date_from)

    # Month/Year selector (above Start Date and End Date)
    months = ['January', 'February', 'March', 'April', 'May', 'June',
              'July', 'August', 'September', 'October', 'November', 'December']
    years = list(range(date.today().year - 5, date.today().year + 3))
    if st.session_state.date_from.year not in years:
        years = sorted(set(years) | {st.session_state.date_from.year})
    year_idx = years.index(st.session_state.date_from.year)
    col_m, col_y = st.columns(2)
    with col_m:
        sel_month = st.selectbox(
            "Month",
            options=months,
            index=st.session_state.date_from.month - 1,
            key='month_select'
        )
    with col_y:
        sel_year = st.selectbox(
            "Year",
            options=years,
            index=year_idx,
            key='year_select'
        )
    # When month/year changes, update date_from and date_to to that month's range
    # (Start/End Date hidden for cleaner UI; values used by report logic)
    new_month = months.index(sel_month) + 1
    new_year = sel_year
    if (new_month, new_year) != (st.session_state.date_from.month, st.session_state.date_from.year):
        st.session_state.date_from = date(new_year, new_month, 1)
        st.session_state.date_to = get_last_day_of_month(st.session_state.date_from)
    # Ensure date_to stays in sync with date_from
    current_date_from = st.session_state.date_from
    target_end = get_last_day_of_month(current_date_from)
    if (st.session_state.date_to.year != current_date_from.year or
        st.session_state.date_to.month != current_date_from.month):
        st.session_state.date_to = target_end

@st.fragment
def CR_btn_1_fragment():
    ###
    if 'selected_cname' not in st.session_state:
        st.session_state.selected_cname = None 
    # if 'display_df6s' not in st.session_state:
    #     st.session_state.display_df6s = pd.DataFrame(columns=['Name']) 
    if 'display_df_name' not in st.session_state:
        st.session_state.display_df_name = pd.DataFrame(columns=['Name', 'dept_code', 'Days'])   ## adjust to Company Name with Days
    # Ensure dept_code and Days columns exist for backward compatibility
    if 'dept_code' not in st.session_state.display_df_name.columns:
        st.session_state.display_df_name['dept_code'] = ''
    if 'Days' not in st.session_state.display_df_name.columns:
        st.session_state.display_df_name['Days'] = 30  # Default to 30 for existing entries
    # Deduplicate display_df_name by Name only (keep one row per customer, no dept)
    if not st.session_state.display_df_name.empty:
        dn = st.session_state.display_df_name.copy()
        dn['_name_key'] = dn['Name'].astype(str).str.strip().str.casefold()
        st.session_state.display_df_name = dn.drop_duplicates(subset=['_name_key'], keep='first').drop(columns=['_name_key']).reset_index(drop=True)
        st.session_state.display_df_name['dept_code'] = ''  # Clear dept - we match by name only
    
    # Check if display_df6_view exists, if not, return early (it will be initialized in main code)
    if 'display_df6_view' not in st.session_state or st.session_state.display_df6_view.empty:
        return
    
    # Ensure ADD Days column exists
    if 'ADD Days' not in st.session_state.display_df6_view.columns:
        st.session_state.display_df6_view['ADD Days'] = 0
    
    # Resolve dept_code column name (display_df6_view may have 'dept_code' or 'DEPT CODE')
    dept_col = 'dept_code' if 'dept_code' in st.session_state.display_df6_view.columns else ('DEPT CODE' if 'DEPT CODE' in st.session_state.display_df6_view.columns else None)
    if dept_col:
        st.session_state.df_add_30days = st.session_state.display_df6_view[['Name', dept_col]].copy()
        st.session_state.df_add_30days = st.session_state.df_add_30days.rename(columns={dept_col: 'dept_code'})
    else:
        st.session_state.df_add_30days = st.session_state.display_df6_view[['Name']].copy()
        st.session_state.df_add_30days['dept_code'] = ''
    # Get unique (Name, dept_code) - no duplication
    st.session_state.df_add_30days = st.session_state.df_add_30days.drop_duplicates(subset=['Name', 'dept_code']).reset_index(drop=True)
    # Remove blank or null Name and normalize
    st.session_state.df_add_30days = st.session_state.df_add_30days[st.session_state.df_add_30days['Name'].notna() & (st.session_state.df_add_30days['Name'] != '')]
    st.session_state.df_add_30days['Name'] = st.session_state.df_add_30days['Name'].str.strip()
    st.session_state.df_add_30days['dept_code'] = st.session_state.df_add_30days['dept_code'].fillna('').astype(str).str.strip()
    # Sort by Name then dept_code
    st.session_state.df_add_30days = st.session_state.df_add_30days.sort_values(by=['Name', 'dept_code']).reset_index(drop=True)

    # Create a placeholder for notifications
    # notification_placeholder = st.empty()
    
    col1, col3, col2 = st.columns([2.2, 2.5, 1])

    with col1: 
        cname_summary = st.session_state.df_add_30days.copy()
    
        # Create selectbox options: Customer Name only (unique names)
        cname_options = cname_summary['Name'].drop_duplicates().tolist()

        # Add empty option at the beginning to make selectbox blank by default
        cname_options = [""] + cname_options

        # Create two columns for selectboxes to be side by side in same container - prevent overlap
        # Using tighter ratios to ensure both fit within col1 without overflowing
        selectbox_col1, selectbox_col2 = st.columns([4, 0.8], gap="small")
        
        with selectbox_col1:
            # Selectbox for choosing Customer Name only
            selected_cname = st.selectbox("Select Customer Name (add-days)", cname_options, index=0, key="cname_selectbox_add_30days")
            st.session_state.selected_cname = selected_cname if selected_cname else None
        
        with selectbox_col2:
            # Selectbox for choosing Add days - compact to prevent overlap
            if 'selected_add_days' not in st.session_state:
                st.session_state.selected_add_days = 30
            selected_add_days = st.selectbox("Add days", [30, 60, 90], index=0, key="add_days_selectbox")
            st.session_state.selected_add_days = selected_add_days

        # Selected value is Customer Name only; dept_code resolved when adding/deleting
        selected_c_name = selected_cname.strip() if selected_cname else ""
        # Resolve dept_code for add: first matching row in cname_summary for this Name (case-insensitive)
        _name_match = cname_summary['Name'].astype(str).str.strip().str.casefold() == selected_c_name.casefold()
        _add_row = cname_summary[_name_match].iloc[0:1]
        selected_dept_code_add = _add_row['dept_code'].iloc[0] if not _add_row.empty else ""
        if pd.isna(selected_dept_code_add):
            selected_dept_code_add = ""
        else:
            selected_dept_code_add = str(selected_dept_code_add).strip()

        
        xcol1, xcol2, xcol3 = st.columns([0.5,0.5,3])
        with xcol1:
            if st.button("➕", key="btn_add_name", help="Add selected customer name with the specified add days to the list"):
                # Check if a customer is selected (not blank)
                if not selected_c_name or selected_c_name.strip() == "":
                    st.warning("Please select a customer name first.")
                else:
                    # Match by Customer Name only (no dept) - add single entry per customer
                    name_match_cs = cname_summary['Name'].astype(str).str.strip().str.casefold() == selected_c_name.strip().casefold()
                    if not name_match_cs.any():
                        st.warning(f"Customer '{selected_c_name}' not found in the data.")
                    else:
                        add_days_value = st.session_state.selected_add_days if 'selected_add_days' in st.session_state else 30
                        c_name_val = selected_c_name.strip()
                        
                        # Add only ONE entry per customer name (no dept) - match by name only
                        dn_names = st.session_state.display_df_name['Name'].astype(str).str.strip().str.casefold()
                        already = dn_names == c_name_val.casefold()
                        if not already.any():
                            new_row = pd.DataFrame({'Name': [c_name_val], 'dept_code': [''], 'Days': [add_days_value]})
                            st.session_state.display_df_name = pd.concat(
                                [st.session_state.display_df_name, new_row],
                                ignore_index=True
                            )
                        else:
                            st.session_state.display_df_name.loc[already, 'Days'] = add_days_value
                        
                        # TASK2: Match by Name only (all rows for this customer, regardless of dept_code)
                        data_rows = st.session_state.display_df6_view.iloc[:-1].copy()  # Exclude grand total
                        
                        # Ensure ADD Days column exists and is numeric
                        if 'ADD Days' not in data_rows.columns:
                            data_rows['ADD Days'] = 0
                        data_rows['ADD Days'] = pd.to_numeric(data_rows['ADD Days'], errors='coerce').fillna(0).astype(int)
                        
                        # Match by Name only - update ALL rows for this customer (consistent with modal)
                        # Do not apply add_days to rows with negative Balance Due / Remaining Balance / BalanceDue
                        data_rows['Name'] = data_rows['Name'].astype(str).str.strip()
                        name_match = data_rows['Name'].str.casefold() == selected_c_name.strip().casefold()
                        bal_col_cr = next((c for c in ['Balance Due', 'Remaining Balance', 'BalanceDue'] if c in data_rows.columns), None)
                        balance_positive_cr = pd.to_numeric(data_rows[bal_col_cr], errors='coerce').fillna(0) >= 0 if bal_col_cr else pd.Series([True] * len(data_rows), index=data_rows.index)
                        matching_mask = name_match & balance_positive_cr  # Match by name only; skip negative balance
                        num_rows = len(data_rows[matching_mask])
                        
                        if num_rows == 0:
                            st.warning(f"No rows found matching customer name: {selected_c_name}")
                        else:
                            # Get the selected add days value (default to 30 if not set)
                            add_days_value = st.session_state.selected_add_days if 'selected_add_days' in st.session_state else 30
                            # Set ADD Days to selected value for matching rows (positive balance only)
                            data_rows.loc[matching_mask, 'ADD Days'] = add_days_value
                            
                            # Ensure Due Date and Original Due Date are datetime
                            data_rows['Due Date'] = pd.to_datetime(data_rows['Due Date'], errors='coerce')
                            if 'Original Due Date' in data_rows.columns:
                                data_rows['Original Due Date'] = pd.to_datetime(data_rows['Original Due Date'], errors='coerce')
                            
                            # Store Original Due Dates before adjustment (for restoration after update_calculations)
                            if 'Original Due Date' in data_rows.columns:
                                data_rows['Original Due Date'] = pd.to_datetime(data_rows['Original Due Date'], errors='coerce')
                            data_rows['Due Date'] = pd.to_datetime(data_rows['Due Date'], errors='coerce')
                            
                            # Directly adjust Due Date by +30 days for ALL rows with the selected customer name
                            # Use Original Due Date as base if available, otherwise use current Due Date
                            # Store adjusted dates with Entry No_ for accurate restoration after update_calculations
                            adjusted_due_dates_map = {}  # {Entry No_: adjusted_due_date}
                            matching_indices = data_rows[matching_mask].index
                            
                            for idx in matching_indices:
                                if 'Original Due Date' in data_rows.columns and pd.notna(data_rows.loc[idx, 'Original Due Date']):
                                    base_date = data_rows.loc[idx, 'Original Due Date']
                                else:
                                    base_date = data_rows.loc[idx, 'Due Date']
                                
                                if pd.notna(base_date):
                                    adjusted_date = base_date + timedelta(days=add_days_value)
                                    data_rows.loc[idx, 'Due Date'] = adjusted_date
                                    # Store with Entry No_ for accurate matching after update_calculations
                                    if 'Entry No_' in data_rows.columns:
                                        entry_no = data_rows.loc[idx, 'Entry No_']
                                        adjusted_due_dates_map[entry_no] = adjusted_date
                            
                            # Preserve any extra columns (like renamed DETAIL_ITEM_NAME) before update_calculations
                            extra_cols = {}
                            for col in data_rows.columns:
                                extra_cols[col] = data_rows[col].copy()
                            
                            # Recalculate data rows (this will recalculate aging buckets based on adjusted Due Date)
                            updated_df = update_calculations(data_rows)
                            
                            # Restore the adjusted Due Date and ADD Days for ALL rows with the selected Name (match by name only, positive balance only)
                            # (update_calculations might have reset them based on payment terms); use case-insensitive name match
                            updated_df['Name'] = updated_df['Name'].astype(str).str.strip()
                            name_match_ud = updated_df['Name'].str.casefold() == selected_c_name.strip().casefold()
                            bal_col_ud = next((c for c in ['Balance Due', 'Remaining Balance', 'BalanceDue'] if c in updated_df.columns), None)
                            balance_positive_ud = pd.to_numeric(updated_df[bal_col_ud], errors='coerce').fillna(0) >= 0 if bal_col_ud else pd.Series([True] * len(updated_df), index=updated_df.index)
                            selected_mask = name_match_ud & balance_positive_ud  # Match by name only; skip negative balance
                            
                            # Ensure ADD Days stays at selected value for ALL matching rows (positive balance only)
                            if 'ADD Days' not in updated_df.columns:
                                updated_df['ADD Days'] = 0
                            updated_df.loc[selected_mask, 'ADD Days'] = add_days_value
                            updated_df['ADD Days'] = pd.to_numeric(updated_df['ADD Days'], errors='coerce').fillna(0).astype(int)
                            updated_df.loc[selected_mask, 'ADD Days'] = add_days_value
                            
                            # Restore the adjusted Due Dates for ALL rows with the selected name
                            updated_df['Due Date'] = pd.to_datetime(updated_df['Due Date'], errors='coerce')
                            
                            if 'Entry No_' in updated_df.columns and len(adjusted_due_dates_map) > 0:
                                # Match by Entry No_ for accurate restoration
                                for entry_no, adj_date in adjusted_due_dates_map.items():
                                    entry_mask = (updated_df['Entry No_'] == entry_no) & selected_mask
                                    if entry_mask.any():
                                        updated_df.loc[entry_mask, 'Due Date'] = adj_date
                            else:
                                # Fallback: recalculate from Original Due Date for each row
                                if 'Original Due Date' in updated_df.columns:
                                    updated_df['Original Due Date'] = pd.to_datetime(updated_df['Original Due Date'], errors='coerce')
                                    for idx in updated_df[selected_mask].index:
                                        if pd.notna(updated_df.loc[idx, 'Original Due Date']):
                                            updated_df.loc[idx, 'Due Date'] = updated_df.loc[idx, 'Original Due Date'] + timedelta(days=add_days_value)
                                        elif pd.notna(updated_df.loc[idx, 'Due Date']):
                                            updated_df.loc[idx, 'Due Date'] = updated_df.loc[idx, 'Due Date'] + timedelta(days=add_days_value)
                                else:
                                    for idx in updated_df[selected_mask].index:
                                        if pd.notna(updated_df.loc[idx, 'Due Date']):
                                            updated_df.loc[idx, 'Due Date'] = updated_df.loc[idx, 'Due Date'] + timedelta(days=add_days_value)
                            
                            # Recalculate aging and age buckets based on the restored Due Date (use matching_indices so we don't rely on dept_code in updated_df)
                            reference_date = updated_df['AsOfDate'].dropna().iloc[0] if not updated_df['AsOfDate'].dropna().empty else None
                            indices_to_update = [i for i in matching_indices if i in updated_df.index]
                            if reference_date and indices_to_update:
                                sel = updated_df.loc[indices_to_update].copy()
                                updated_df.loc[indices_to_update, 'AgingDays'] = sel.apply(
                                    lambda row: (reference_date - row['Due Date']).days if pd.notna(row['Due Date']) else None,
                                    axis=1
                                )
                                sel = updated_df.loc[indices_to_update]  # refresh so bucket calcs use updated AgingDays
                                updated_df.loc[indices_to_update, 'Current'] = sel.apply(
                                    lambda row: row['Balance Due'] if row['AgingDays'] is not None and row['AgingDays'] < 1 else None, axis=1
                                )
                                updated_df.loc[indices_to_update, 'Days_1_to_30'] = sel.apply(
                                    lambda row: row['Balance Due'] if row['AgingDays'] is not None and 1 <= row['AgingDays'] <= 30 else None, axis=1
                                )
                                updated_df.loc[indices_to_update, 'Days_31_to_60'] = sel.apply(
                                    lambda row: row['Balance Due'] if row['AgingDays'] is not None and 31 <= row['AgingDays'] <= 60 else None, axis=1
                                )
                                updated_df.loc[indices_to_update, 'Days_61_to_90'] = sel.apply(
                                    lambda row: row['Balance Due'] if row['AgingDays'] is not None and 61 <= row['AgingDays'] <= 90 else None, axis=1
                                )
                                updated_df.loc[indices_to_update, 'Over_91_Days'] = sel.apply(
                                    lambda row: row['Balance Due'] if row['AgingDays'] is not None and row['AgingDays'] >= 91 else None, axis=1
                                )
                                updated_df.loc[indices_to_update, 'Total Target'] = sel.apply(
                                    lambda row: row['Balance Due'] if row['AgingDays'] is not None and row['AgingDays'] > 0 else 0, axis=1
                                )
                            
                            # Re-add preserved extra columns that aren't in the output
                            for col_name, col_data in extra_cols.items():
                                if col_name not in updated_df.columns and len(col_data) == len(updated_df):
                                    updated_df[col_name] = col_data.values
                            
                            # Reattach grand total row
                            grand_total = st.session_state.display_df6_view.iloc[-1:].copy()
                            final_df = pd.concat([updated_df, grand_total], ignore_index=True)
                            
                            # Ensure ADD Days is preserved as integer in final dataframe
                            if 'ADD Days' in final_df.columns:
                                final_df['ADD Days'] = pd.to_numeric(final_df['ADD Days'], errors='coerce').fillna(0).astype(int)
                            
                            # Re-apply ADD Days to selected rows in final_df (by Name only, positive balance only); case-insensitive name match
                            final_df['Name'] = final_df['Name'].astype(str).str.strip()
                            fn_match = final_df['Name'].str.casefold() == selected_c_name.strip().casefold()
                            bal_col_fn = next((c for c in ['Balance Due', 'Remaining Balance', 'BalanceDue'] if c in final_df.columns), None)
                            if bal_col_fn:
                                fn_match = fn_match & (pd.to_numeric(final_df[bal_col_fn], errors='coerce').fillna(0) >= 0)
                            final_df.loc[fn_match, 'ADD Days'] = add_days_value
                            
                            # Update session state with the final dataframe
                            st.session_state.display_df6_view = final_df.copy()
                            
                            # TASK1: Explicitly sync display_df6_view_state so st.data_editor reflects changes immediately
                            sync_df = final_df.copy()
                            date_columns_sync = ['Posting Date', 'Due Date', 'AsOfDate', 'Original Due Date']
                            for col in date_columns_sync:
                                if col in sync_df.columns:
                                    if pd.api.types.is_datetime64_any_dtype(sync_df[col]):
                                        sync_df[col] = sync_df[col].dt.strftime('%Y-%m-%d')
                                    elif sync_df[col].dtype == 'object':
                                        try:
                                            sync_df[col] = pd.to_datetime(sync_df[col], errors='coerce').dt.strftime('%Y-%m-%d')
                                        except (ValueError, TypeError):
                                            pass
                            st.session_state.display_df6_view_state = sync_df.copy()
                    
        with xcol2:
            if st.button("➖", key="btn_del_name", help="Remove selected customer name from the list and reset add days to 0"):
                # Check if a customer is selected (not blank)
                if not selected_c_name or selected_c_name.strip() == "":
                    st.warning("Please select a customer name first.")
                else:
                    # TASK2: Remove ALL (Name, dept_code) for this customer from display_df_name
                    dn = st.session_state.display_df_name
                    dn_name_match = dn['Name'].astype(str).str.strip().str.casefold() == selected_c_name.strip().casefold()
                    if not dn_name_match.any():
                        st.warning(f"Customer '{selected_c_name}' is not in the add-days list.")
                    else:
                        # Get max add_days from any removed entry for Due Date rollback (use max in case different depts had different days)
                        removed = dn[dn_name_match]
                        add_days_to_undo = int(removed['Days'].max()) if not removed.empty and 'Days' in removed.columns else 0
                        keep = ~dn_name_match
                        st.session_state.display_df_name = dn[keep].reset_index(drop=True)
                        # Reset ADD Days to 0 for ALL rows matching this customer (by Name only)
                        data_rows = st.session_state.display_df6_view.iloc[:-1].copy()  # Exclude grand total
                        data_rows['Name'] = data_rows['Name'].astype(str).str.strip()
                        del_name_match = data_rows['Name'].str.casefold() == selected_c_name.strip().casefold()
                        matching_mask = del_name_match  # Match by name only (all dept_codes for this customer)
                        num_rows = int(matching_mask.sum())
                        # Store indices of rows to reset (update_calculations returns subset of columns and may drop dept_code; row order is preserved)
                        matching_indices = data_rows.index[matching_mask].tolist()
                        
                        # Reset ADD Days to 0 only for matching rows
                        data_rows.loc[matching_mask, 'ADD Days'] = 0
                        
                        # Reset Due Date: use Original Due Date when available, else subtract add_days_to_undo from current Due Date so age buckets adjust correctly
                        data_rows['Due Date'] = pd.to_datetime(data_rows['Due Date'], errors='coerce')
                        if 'Original Due Date' in data_rows.columns:
                            data_rows['Original Due Date'] = pd.to_datetime(data_rows['Original Due Date'], errors='coerce')
                            # Restore Original Due Date for matching rows where available
                            has_orig = matching_mask & data_rows['Original Due Date'].notna()
                            data_rows.loc[has_orig, 'Due Date'] = data_rows.loc[has_orig, 'Original Due Date']
                            # Where Original Due Date is null, roll back by subtracting the add_days that was applied
                            no_orig = matching_mask & data_rows['Original Due Date'].isna()
                            if no_orig.any() and add_days_to_undo and pd.notna(data_rows.loc[no_orig, 'Due Date']).any():
                                data_rows.loc[no_orig, 'Due Date'] = data_rows.loc[no_orig, 'Due Date'] - timedelta(days=add_days_to_undo)
                        else:
                            # No Original Due Date column: roll back by subtracting add_days_to_undo for matching rows
                            if add_days_to_undo:
                                data_rows.loc[matching_mask, 'Due Date'] = data_rows.loc[matching_mask, 'Due Date'] - timedelta(days=add_days_to_undo)
                        
                        # Set temporary notification
                        st.session_state.notification = f"Reset {num_rows} rows for Name: {selected_c_name}"
                        st.session_state.notification_time = time.time()
                        
                        # Preserve any extra columns (like renamed DETAIL_ITEM_NAME) before update_calculations
                        extra_cols = {}
                        for col in data_rows.columns:
                            extra_cols[col] = data_rows[col].copy()
                        
                        # Recalculate data rows (this will recalculate aging buckets based on reset Due Date)
                        updated_df = update_calculations(data_rows)
                        
                        # Ensure ADD Days stays at 0 only for the selected (Name, dept_code) using stored indices
                        # (update_calculations returns df[available_columns] and may drop dept_code, so we use indices instead of re-matching by Name/dept_code)
                        for idx in matching_indices:
                            if idx in updated_df.index:
                                updated_df.loc[idx, 'ADD Days'] = 0
                        
                        # Re-add preserved extra columns that aren't in the output
                        for col_name, col_data in extra_cols.items():
                            if col_name not in updated_df.columns and len(col_data) == len(updated_df):
                                updated_df[col_name] = col_data.values
                        
                        # Reattach grand total row
                        grand_total = st.session_state.display_df6_view.iloc[-1:].copy()
                        final_df_del = pd.concat([updated_df, grand_total], ignore_index=True)
                        st.session_state.display_df6_view = final_df_del
                        
                        # TASK1: Sync display_df6_view_state so st.data_editor reflects delete changes
                        sync_df_del = final_df_del.copy()
                        date_columns_sync = ['Posting Date', 'Due Date', 'AsOfDate', 'Original Due Date']
                        for col in date_columns_sync:
                            if col in sync_df_del.columns:
                                if pd.api.types.is_datetime64_any_dtype(sync_df_del[col]):
                                    sync_df_del[col] = sync_df_del[col].dt.strftime('%Y-%m-%d')
                                elif sync_df_del[col].dtype == 'object':
                                    try:
                                        sync_df_del[col] = pd.to_datetime(sync_df_del[col], errors='coerce').dt.strftime('%Y-%m-%d')
                                    except (ValueError, TypeError):
                                        pass
                        st.session_state.display_df6_view_state = sync_df_del.copy()
      
    with col2: # AR With Add Days - 4 buttons in 2x2 grid
        st.markdown("""
        <style>
        /* Compact button labels - smallest font to reduce wrapping in AR action buttons grid */
        div[data-testid="column"]:has(#ar-btn-grid-marker) .stButton > button {
            font-size: 0.7rem !important;
            line-height: 1.1 !important;
            padding: 0.2rem 0.35rem !important;
        }
        </style>
        <div id="ar-btn-grid-marker"></div>
        """, unsafe_allow_html=True)
        btn_row1_a, btn_row1_b = st.columns(2)
        with btn_row1_a:
            if st.button("A/R Add Days", key="btn_ar_with_add_days", use_container_width=True, help="A/R with Add Days"):
                AR_with_Add_Days_modal_fragment()
        with btn_row1_b:
            if st.button("Edit Conditions", key="btn_edit_conditions", use_container_width=True):
                edit_conditions_modal_fragment()
        btn_row2_a, btn_row2_b = st.columns(2)
        with btn_row2_a:
            if st.button("Re-tag History", key="btn_re_tag_history", use_container_width=True):
                re_tag_history_modal_fragment()
        with btn_row2_b:
            if st.button("Default Add Days", key="btn_default_customer_add_days", use_container_width=True, help="Default Customer with Add Days (from sproc8a)"):
                default_customer_add_days_modal_fragment()

            
    with col3:
        # Display the selected Names as a list with days (Customer Name only, no dept)
        if not st.session_state.display_df_name.empty:  
            st.markdown("##### Currently Listed Customer Names w/ Add-days:")      
            # Deduplicate by name (case-insensitive) - show each customer name once
            seen_names = set()
            for _, row in st.session_state.display_df_name.iterrows():
                c_name = row['Name'].strip() if pd.notna(row['Name']) else ''
                if not c_name:
                    continue
                name_key = c_name.casefold()
                if name_key in seen_names:
                    continue
                seen_names.add(name_key)
                days_value = row.get('Days', 30)  # Default to 30 if Days column doesn't exist
                st.caption(f"• {c_name} +{int(days_value)}")  # Display name only (no dept)
        else:
            st.markdown("##### \n*No Name selected yet.*")
        
        # Add horizontal line for separation
        # st.markdown("---")    
    
    # Display the dataframe INSIDE the fragment so it reads directly from updated session state
    # Read fresh from session state
    display_df6_view = st.session_state.display_df6_view.copy()
    
    # Convert all date/datetime columns to strings for PyArrow compatibility
    # This prevents ArrowTypeError when displaying in st.dataframe
    date_columns = ['Posting Date', 'Due Date', 'AsOfDate', 'Original Due Date']
    for col in date_columns:
        if col in display_df6_view.columns:
            if pd.api.types.is_datetime64_any_dtype(display_df6_view[col]):
                display_df6_view[col] = display_df6_view[col].dt.strftime('%Y-%m-%d')
            elif display_df6_view[col].dtype == 'object':
                # Check if it contains date objects
                try:
                    # Try to convert to datetime first, then to string
                    display_df6_view[col] = pd.to_datetime(display_df6_view[col], errors='coerce').dt.strftime('%Y-%m-%d')
                except (ValueError, TypeError):  # noqa: E722
                    pass  # If conversion fails, leave as is
    
    # Initialize/update the editable state for data_editor
    # Sync from the main session state to ensure we have the latest data
    # This includes any SR2 edits that were applied via df_on_change_sr2()
    if 'display_df6_view_state' not in st.session_state:
        st.session_state.display_df6_view_state = display_df6_view.copy()
    else:
        # Update state if the source dataframe has changed (shape or structure)
        # This ensures we sync when display_df6_view is updated elsewhere (not through editor)
        # Note: df_on_change_sr2() already updates both, so they should be in sync after edits
        current_shape = (len(display_df6_view), len(display_df6_view.columns))
        state_shape = (len(st.session_state.display_df6_view_state), len(st.session_state.display_df6_view_state.columns))
        
        if current_shape != state_shape:
            # Shape changed, need to sync
            st.session_state.display_df6_view_state = display_df6_view.copy()
        elif not st.session_state.display_df6_view_state.equals(display_df6_view):
            # Shape is same but content differs - sync to reflect updates from other sources
            # (df_on_change_sr2 updates both, so this mainly handles updates from other code paths)
            st.session_state.display_df6_view_state = display_df6_view.copy()
    
    if DEBUG_SR_CODE2:
        _sr2_debug_log("[FRAGMENT] CR_btn_1_fragment running")
    # Apply Re-tag History before displaying: updates SR2 and SR_Code2 where Entry No_ and Original SR2 match history
    st.session_state.display_df6_view_state = apply_re_tag_history_to_df(st.session_state.display_df6_view_state)
    # Apply DSS Re-tag History: updates DSS_NAME and DSS where Entry No_ and Original DSS_Name match history
    st.session_state.display_df6_view_state = apply_re_tag_history_dss_to_df(st.session_state.display_df6_view_state)
    
    # WORKAROUND: on_change callback may not fire when data_editor is inside @st.fragment.
    # Use widget state (display_df6_editor) as source when it has newer SR2 or DSS_NAME edits than display_df6_view_state.
    _sr2_diffs_detected = []
    _dss_diffs_detected = []
    _widget_has_dataframe = False
    if "display_df6_editor" in st.session_state:
        widget_state = st.session_state["display_df6_editor"]
        prev_state = st.session_state.display_df6_view_state
        if hasattr(widget_state, 'loc') and hasattr(widget_state, 'columns') and 'SR2' in widget_state.columns:
            # Widget state is DataFrame format
            _widget_has_dataframe = True
            if len(widget_state) == len(prev_state):
                # Find rows where SR2 changed in widget vs our state
                for idx in range(min(len(widget_state), len(prev_state))):
                    w_sr2 = str(widget_state.iloc[idx]['SR2']).strip() if pd.notna(widget_state.iloc[idx].get('SR2')) else ''
                    p_sr2 = str(prev_state.iloc[idx]['SR2']).strip() if pd.notna(prev_state.iloc[idx].get('SR2')) else ''
                    if w_sr2 != p_sr2:
                        _sr2_diffs_detected.append(idx)
                # Find rows where DSS_NAME changed (if column exists)
                if 'DSS_NAME' in widget_state.columns and 'DSS_NAME' in prev_state.columns:
                    for idx in range(min(len(widget_state), len(prev_state))):
                        w_dss = str(widget_state.iloc[idx]['DSS_NAME']).strip() if pd.notna(widget_state.iloc[idx].get('DSS_NAME')) else ''
                        p_dss = str(prev_state.iloc[idx]['DSS_NAME']).strip() if pd.notna(prev_state.iloc[idx].get('DSS_NAME')) else ''
                        if w_dss != p_dss:
                            _dss_diffs_detected.append(idx)
                if _sr2_diffs_detected or _dss_diffs_detected:
                    if DEBUG_SR_CODE2:
                        _sr2_debug_log(f"[FRAGMENT] Widget has SR2 diffs at rows {_sr2_diffs_detected[:5]}... DSS diffs at {_dss_diffs_detected[:5]}... - applying via fragment (callback did not fire)")
                    merged = widget_state.copy()
                    merged = _sync_sr_code2_from_sr2(merged)
                    merged = _sync_dss_from_dss_name(merged)
                    st.session_state.display_df6_view_state = merged
                    st.session_state.display_df6_view = merged.copy()
                    _write_sr2_edits_to_history(merged, _sr2_diffs_detected, prev_state)
                    _write_dss_edits_to_history(merged, _dss_diffs_detected, prev_state)
                    st.session_state._sr2_workaround_applied = True
                    st.rerun()
        elif isinstance(widget_state, dict):
            # Widget state is dict format (edited_rows) - build edited df and detect diffs
            edited_rows = widget_state.get("edited_rows", {})
            if edited_rows and len(prev_state) > 0:
                merged = prev_state.copy()
                for idx, updates in edited_rows.items():
                    idx = int(idx) if isinstance(idx, str) else idx
                    if not isinstance(updates, dict) or idx < 0 or idx >= len(merged):
                        continue
                    if 'SR2' in updates and updates['SR2'] is not None:
                        p_sr2 = str(merged.loc[merged.index[idx], 'SR2']).strip() if pd.notna(merged.loc[merged.index[idx], 'SR2']) else ''
                        w_sr2 = str(updates['SR2']).strip() if pd.notna(updates['SR2']) else ''
                        if w_sr2 != p_sr2:
                            _sr2_diffs_detected.append(idx)
                        merged.loc[merged.index[idx], 'SR2'] = updates['SR2']
                    if 'DSS_NAME' in updates and updates['DSS_NAME'] is not None and 'DSS_NAME' in merged.columns:
                        p_dss = str(merged.loc[merged.index[idx], 'DSS_NAME']).strip() if pd.notna(merged.loc[merged.index[idx], 'DSS_NAME']) else ''
                        w_dss = str(updates['DSS_NAME']).strip() if pd.notna(updates['DSS_NAME']) else ''
                        if w_dss != p_dss:
                            _dss_diffs_detected.append(idx)
                        merged.loc[merged.index[idx], 'DSS_NAME'] = updates['DSS_NAME']
                if _sr2_diffs_detected or _dss_diffs_detected:
                    if DEBUG_SR_CODE2:
                        _sr2_debug_log(f"[FRAGMENT] Widget (dict) has SR2 diffs at {_sr2_diffs_detected[:5]}... DSS diffs at {_dss_diffs_detected[:5]}... - applying via fragment")
                    merged = _sync_sr_code2_from_sr2(merged)
                    merged = _sync_dss_from_dss_name(merged)
                    st.session_state.display_df6_view_state = merged
                    st.session_state.display_df6_view = merged.copy()
                    _write_sr2_edits_to_history(merged, _sr2_diffs_detected, prev_state)
                    _write_dss_edits_to_history(merged, _dss_diffs_detected, prev_state)
                    st.session_state._sr2_workaround_applied = True
                    st.rerun()
    
    # Force SR_CODE2 to match SR2 and DSS to match DSS_NAME for every row (catches callback edits that may not persist through fragment rerun)
    st.session_state.display_df6_view_state = _sync_sr_code2_from_sr2(st.session_state.display_df6_view_state)
    st.session_state.display_df6_view_state = _sync_dss_from_dss_name(st.session_state.display_df6_view_state)
    if DEBUG_SR_CODE2:
        _sc = 'SR_CODE2' if 'SR_CODE2' in st.session_state.display_df6_view_state.columns else ('SR_Code2' if 'SR_Code2' in st.session_state.display_df6_view_state.columns else None)
        _sr2_debug_log(f"[FRAGMENT] After _sync_sr_code2_from_sr2 | col={_sc} | sample SR2={st.session_state.display_df6_view_state['SR2'].iloc[:5].tolist()} SR_CODE2={st.session_state.display_df6_view_state[_sc].iloc[:5].tolist() if _sc else 'N/A'}")
    
    # Build unique SR2 + CR_Name options for SR2 dropdown (include current values for display)
    _df = st.session_state.display_df6_view_state
    _data = _df.iloc[:-1] if len(_df) > 1 else _df
    sr2_options = set(str(v).strip() for v in _data['SR2'].dropna().unique() if str(v).strip())
    cr_name_col = next((c for c in ['CR_NAME', 'CR_Name'] if c in _data.columns), None)
    if cr_name_col:
        sr2_options.update(str(v).strip() for v in _data[cr_name_col].dropna().unique() if str(v).strip())
    try:
        hist = pd.read_csv('re_tag_history.csv')
        if not hist.empty and 'SR2' in hist.columns:
            sr2_options.update(str(v).strip() for v in hist['SR2'].dropna().unique() if str(v).strip())
    except FileNotFoundError:
        pass
    sr2_options = sorted(sr2_options)

    # Build unique DSS_NAME options for DSS_NAME dropdown (include current values and re_tag_history_dss)
    dss_name_options = []
    if 'DSS_NAME' in _data.columns:
        dss_name_options = sorted(set(str(v).strip() for v in _data['DSS_NAME'].dropna().unique() if str(v).strip()))
        try:
            hist_dss = pd.read_csv('re_tag_history_dss.csv')
            dss_name_hist_col = 'DSS_Name' if 'DSS_Name' in hist_dss.columns else 'DSS_NAME'
            if not hist_dss.empty and dss_name_hist_col in hist_dss.columns:
                dss_name_options = sorted(set(dss_name_options) | set(str(v).strip() for v in hist_dss[dss_name_hist_col].dropna().unique() if str(v).strip()))
        except FileNotFoundError:
            pass
    dss_name_options = dss_name_options if dss_name_options else ['']
    
    # --- SR_CODE2 TROUBLESHOOTING UI (remove when DEBUG_SR_CODE2=False) ---
    if DEBUG_SR_CODE2:
        _df = st.session_state.display_df6_view_state
        _data = _df.iloc[:-1] if len(_df) > 1 else _df
        _sc_col = 'SR_CODE2' if 'SR_CODE2' in _data.columns else ('SR_Code2' if 'SR_Code2' in _data.columns else None)
        _cr_col = next((c for c in ['CR_NAME', 'CR_Name'] if c in _data.columns), None)
        _cr = next((c for c in ['CR', 'CR_CODE'] if c in _data.columns), None)
        # Build expected SR2->SR_CODE2 mapping (same as _sync_sr_code2_from_sr2)
        _sr2_to_code = {}
        for _sr2 in _data['SR2'].dropna().unique():
            _s = str(_sr2).strip()
            _sub = _data[_data['SR2'].astype(str).str.strip() == _s]
            _codes = []
            for _, _r in _sub.iterrows():
                _crv = str(_r[_cr]).strip() if _cr and _cr in _r.index and pd.notna(_r.get(_cr)) else ''
                _cn = str(_r.get(_cr_col, '')).strip() if _cr_col and _cr_col in _r.index and pd.notna(_r.get(_cr_col)) else ''
                _same = (_cn == _s or (len(_s.split()) >= 2 and len(_cn.split()) >= 2 and _s.split()[:2] == _cn.split()[:2]))
                if _same and _crv:
                    _codes.append(_crv)
                else:
                    _v = _r.get(_sc_col)
                    if pd.notna(_v) and str(_v).strip():
                        _codes.append(str(_v).strip())
            if _codes:
                _sr2_to_code[_s] = _codes[0]
            else:
                _sr2_to_code[_s] = 'ZZZ' if _s == 'Head Office' else '?'
        try:
            _hist = pd.read_csv('re_tag_history.csv')
            if not _hist.empty and 'SR2' in _hist.columns and 'SR_Code2' in _hist.columns:
                for _, _r in _hist.iterrows():
                    _s = str(_r['SR2']).strip() if pd.notna(_r['SR2']) else ''
                    _c = str(_r['SR_Code2']).strip() if pd.notna(_r['SR_Code2']) else ''
                    if _s and _c:
                        _sr2_to_code[_s] = _c
        except FileNotFoundError:
            pass
        # Store debug info for UI (visible even if log file not found)
        if '_sr2_debug_last_run' not in st.session_state:
            st.session_state._sr2_debug_last_run = None
        st.session_state._sr2_debug_last_run = datetime.now().strftime('%H:%M:%S')
        st.session_state._sr2_debug_editor_in_session = "display_df6_editor" in st.session_state
        st.session_state._sr2_debug_widget_is_df = _widget_has_dataframe
        st.session_state._sr2_debug_diffs_count = len(_sr2_diffs_detected)
        with st.expander("🔧 SR_CODE2 Troubleshooting (set DEBUG_SR_CODE2=False to remove)", expanded=True):
            st.warning("**Visible debug (no log file needed):**")
            st.text(f"Fragment last ran: {st.session_state._sr2_debug_last_run} | display_df6_editor in session: {st.session_state._sr2_debug_editor_in_session} | widget is DataFrame: {st.session_state._sr2_debug_widget_is_df} | SR2 diffs detected: {st.session_state._sr2_debug_diffs_count}")
            st.caption("If 'SR2 diffs detected: 0' after selecting SR2, the fragment may not be rerunning when you select from the dropdown.")
            st.info("**Expected SR_CODE2 when you select SR2:** Head Office → ZZZ | Mark Francis Leoncio → SR060 (from CR when CR_Name matches)")
            st.markdown("**SR2 → SR_CODE2 mapping:**")
            for _k, _v in sorted(_sr2_to_code.items()):
                st.text(f"  {_k} → {_v}")
            # Check for mismatches
            _mismatches = []
            for _i in range(len(_data)):
                _sr2 = str(_data.iloc[_i]['SR2']).strip() if pd.notna(_data.iloc[_i]['SR2']) else ''
                _expected = _sr2_to_code.get(_sr2, 'ZZZ' if _sr2 == 'Head Office' else '?')
                _actual = str(_data.iloc[_i].get(_sc_col, '')).strip() if _sc_col and _sc_col in _data.columns else 'N/A'
                if _expected and _expected != '?' and _actual != _expected:
                    _mismatches.append(f"Row {_i+1}: SR2={_sr2} | Expected SR_CODE2={_expected} | Actual={_actual}")
            if _mismatches:
                st.warning("**Mismatches (SR_CODE2 not updated):**")
                for _m in _mismatches[:10]:
                    st.text(_m)
                if len(_mismatches) > 10:
                    st.caption(f"... and {len(_mismatches)-10} more")
            else:
                st.success("No mismatches found.")
            st.caption("Check sr_code2_debug.log in project folder. If callback logs are missing, fragment workaround applies SR_CODE2 from widget state.")
    
    st.markdown("---")
    # Display the data editor with SR2 and DSS_NAME as dropdowns; selecting updates SR_Code2/DSS and saves to re-tag history
    st.caption("Select SR2 from the dropdown to re-tag. SR_Code2 updates automatically and saves to re-tag history. Select DSS_NAME to re-tag; DSS updates automatically.")
    editable_cols = ['SR2']
    if 'DSS_NAME' in st.session_state.display_df6_view_state.columns:
        editable_cols.append('DSS_NAME')
    disabled_cols = [col for col in st.session_state.display_df6_view_state.columns if col not in editable_cols]
    column_cfg = {
        "SR2": st.column_config.SelectboxColumn(
            "SR2",
            options=sr2_options if sr2_options else ["Head Office"],
            required=True,
            help="Select SR2 to re-tag. SR_Code2 updates automatically."
        )
    }
    if 'DSS_NAME' in st.session_state.display_df6_view_state.columns:
        column_cfg["DSS_NAME"] = st.column_config.SelectboxColumn(
            "DSS_NAME",
            options=dss_name_options if dss_name_options else [''],
            required=False,
            help="Select DSS_NAME to re-tag. DSS updates automatically."
        )
    column_cfg = _numeric_column_config(st.session_state.display_df6_view_state, column_cfg)
    st.data_editor(
        st.session_state.display_df6_view_state,
        use_container_width=True,
        hide_index=True,
        key="display_df6_editor",
        on_change=df_on_change_sr2,
        disabled=disabled_cols,
        column_config=column_cfg
    )

    date_to_str = st.session_state.date_to_str
    if "display_df6_view" in st.session_state:
        # Use the updated session state for downloads (includes SR2 edits)
        download_df = st.session_state.display_df6_view.copy()
        
        # Convert date columns to strings for download compatibility
        date_columns = ['Posting Date', 'Due Date', 'AsOfDate', 'Original Due Date']
        for col in date_columns:
            if col in download_df.columns:
                if pd.api.types.is_datetime64_any_dtype(download_df[col]):
                    download_df[col] = download_df[col].dt.strftime('%Y-%m-%d')
                elif download_df[col].dtype == 'object':
                    try:
                        download_df[col] = pd.to_datetime(download_df[col], errors='coerce').dt.strftime('%Y-%m-%d')
                    except (ValueError, TypeError):  # noqa: E722
                        pass
        
        # Try to create Excel file, fall back to CSV if Excel engines not available
        csv = st.session_state.result_df6.to_csv(index=False)
        st.download_button(
            label="Download Report as CSV (Original Data)",
            data=csv,
            file_name=f"AR Customer Report {date_to_str}.csv",
            mime="text/csv")
        
        try:
            output = BytesIO()
            # Try openpyxl first, then xlsxwriter
            try:
                download_df.to_excel(output, index=False, engine='openpyxl')
            except ImportError:
                download_df.to_excel(output, index=False, engine='xlsxwriter')                  
            excel_data = output.getvalue()
            st.download_button(
                label="Download Report as Excel",
                data=excel_data,
                file_name=f"AR Customer Report {date_to_str}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        except (ImportError, ModuleNotFoundError):
            # Fall back to CSV if no Excel engine is available
            csv = download_df.to_csv(index=False)
            st.download_button(
                label="Download Report as CSV",
                data=csv,
                file_name=f"AR Customer Report {date_to_str}.csv",
                mime="text/csv")
            st.warning("Excel format not available. Install 'openpyxl' or 'xlsxwriter' for Excel downloads. Using CSV format instead.")

@st.fragment
def CR_btn_2_fragment():
    ###
    result_df7 = st.session_state.result_df7.copy()
    date_to_str = st.session_state.date_to_str
    asof_month = (datetime.strptime(date_to_str, '%Y-%m-%d') + relativedelta(months=1,day=31)).strftime('%m-%Y').upper() 
    csv = result_df7.to_csv(index=False)
    st.download_button(
        label="Download Report as CSV",
        data=csv,
        file_name=f"BC365_Collection_Report_for_{asof_month}.csv",
        mime="text/csv") 

@st.fragment
def CR_btn_2_cc_fragment():
    """Fragment for Row-Detailed Collection tab: dataframe (all columns) + download button. Prevents page rerun on download."""
    date_to_str = st.session_state.date_to_str
    asof_month = (datetime.strptime(date_to_str, '%Y-%m-%d') + relativedelta(months=1, day=31)).strftime('%m-%Y').upper()
    if 'result_df7_cc' not in st.session_state or st.session_state.result_df7_cc.empty:
        st.info("No row-detailed collection data available. Please generate the report first.")
        return
    display_df7_cc = st.session_state.result_df7_cc.copy()
    display_df6_cc = st.session_state.display_df6_view_state.copy()
    display_df6_cc.columns = display_df6_cc.columns.str.upper()
    display_df6_cc = display_df6_cc[['CURRENT', 'TOTAL TARGET','DOCUMENT NO_','CUSTOMER NO_','EXTERNAL DOCUMENT NO_']]
    # Merge left on different column names: display_df7_cc keys 'Detailed_Document_No','Customer No_','Detailed_External_Document_No'
    # to display_df6_cc keys 'DOCUMENT NO_','CUSTOMER NO_','EXTERNAL DOCUMENT NO_' to bring in 'CURRENT', 'TOTAL TARGET'
    display_df7_cc = display_df7_cc.merge(
        display_df6_cc,
        # Since 'DOCUMENT TYPE' matches exactly on both sides, it merges into ONE column automatically
        left_on=['Detailed_Document_No', 'Customer No_', 'Detailed_External_Document_No'],
        right_on=['DOCUMENT NO_', 'CUSTOMER NO_', 'EXTERNAL DOCUMENT NO_'],
        how='left',
        suffixes=('', '_1')
    ).drop(columns=['DOCUMENT NO_', 'CUSTOMER NO_', 'EXTERNAL DOCUMENT NO_'])
    # Only 'CURRENT', 'TOTAL TARGET' will be added to the left table
    # If 'DOCUMENT TYPE' not equal to 'PAYMENT' the value in 'CURRENT', 'TOTAL TARGET' will be equal to None
    display_df7_cc['CURRENT'] = display_df7_cc.apply(lambda row: None if row['DOCUMENT TYPE'] != 'PAYMENT' else row['CURRENT'], axis=1)
    display_df7_cc['TOTAL TARGET'] = display_df7_cc.apply(lambda row: None if row['DOCUMENT TYPE'] != 'PAYMENT' else row['TOTAL TARGET'], axis=1)
    
    col_cfg_df7_cc = _numeric_column_config(display_df7_cc)
    st.dataframe(display_df7_cc, use_container_width=True, hide_index=True, column_config=col_cfg_df7_cc)
    csv = display_df7_cc.to_csv(index=False)
    st.download_button(
        label="Download Row-Detailed Collection as CSV",
        data=csv,
        key="download_row_detailed_collection_csv",
        file_name=f"Row_Detailed_Collection_{asof_month}.csv",
        mime="text/csv")

@st.fragment
def CR_btn_ov_fragment():
    ###
    overdue_report = st.session_state.overdue_report
    date_from_str = st.session_state.date_from_str    
    csv = overdue_report.to_csv(index=False)
    st.download_button(
        label="Download OVERDUE Report by CR Name and Customer Name",
        data=csv,
        key="download_current_csv_od",
        file_name=f"Target OVERDUE with Customer AsOf AR [{date_from_str}].csv",
        mime="text/csv")  
    print("Done with OVERDUE") 

@st.fragment
def CR_TARGET_CURRENT_fragment():
    
    date_from_str = st.session_state.date_from_str  # noqa: F841
    date_to_str = st.session_state.date_to_str
    asof_month = (datetime.strptime(date_to_str, '%Y-%m-%d') + relativedelta(months=1,day=31)).strftime('%m-%Y').upper()                    
    df_overdue = st.session_state.display_df6s.copy()
    # df_overdue = st.session_state.display_df6_view.copy()
    df_overdue = df_overdue[['Document No_','External Document No_','ADD Days','Due Date','ITEM CODE','PRODUCT','SR2','SR_CODE2','DSS2_Name','Category']]    
    result_df7z = st.session_state.result_df7.copy()
    
                                        
    display_df7z = result_df7z.copy()                            
    display_df7z = display_df7z[(~display_df7z['BalAccountNo'].str.contains('EWT|WHT', na=False))]

    # display_df7z = display_df7z.drop_duplicates().reset_index(drop=True)
    display_df7z = display_df7z.drop_duplicates(subset=['DocumentNo','ExternalDocumentNo','DetailDoc','CollectedAmount','Collected_EWT'], keep='first').reset_index(drop=True)
    merged_df7_ov = pd.merge(df_overdue, display_df7z, left_on=['Document No_', 'External Document No_'], right_on=['DocumentNo', 'ExternalDocumentNo'], how='inner')

    # merged_df7_ov.to_csv("debug_merged_df7_ov_initial.csv", index=False)  # Debugging line to check the content of merged_df7_ov

    # merged_df7_ov.to_csv("debug_merged_df7_ov_current.csv", index=False)  # Debugging line to check the content of merged_df7_ov 
    merged_df7_ov = merged_df7_ov.drop(columns=['VLookup'], errors='ignore') # EntryNo has been retrieved from dropping
                    
    merged_df7_ov['CollectedAmount'] = pd.to_numeric(merged_df7_ov['CollectedAmount'], errors='coerce')                                              
    merged_df7_ov.rename(columns={
            'CollectedAmount': 'Collected_Amount',
            'PaidUnpaid': 'Remaining Balance',
            'Due Date': 'Adjusted Due Date',
            }, inplace=True)  

    asof_OD = (datetime.strptime(date_to_str, '%Y-%m-%d'))
    # Convert 'Adjusted Due Date' to 'YYYY-MM-DD' string format
    merged_df7_ov['Adjusted Due Date'] = pd.to_datetime(merged_df7_ov['Adjusted Due Date']).dt.strftime('%Y-%m-%d')
    # Convert asof_OD and 'Adjusted Due Date' to datetime for month difference calculation
    date1 = pd.to_datetime(asof_OD)
    # Compute month difference for each row
    # month_diff = merged_df7_ov['Adjusted Due Date'].apply(
    #     lambda x: abs((pd.to_datetime(x).year - date1.year) * 12 + pd.to_datetime(x).month - date1.month) == 1
    #     and pd.to_datetime(x) < date1
    # )

    month_diff = merged_df7_ov['Adjusted Due Date'].apply(lambda x: (date1 - pd.to_datetime(x)).days < 1)

    # Filter rows where month difference is exactly 1 
    merged_df7_ov = merged_df7_ov[month_diff]                  
    # merged_df7_ov = merged_df7_ov[(~merged_df7_ov['DocumentNo'].str.startswith(('PSCM', 'JV'))) & (merged_df7_ov['DocumentType'] == 'INVOICE')]                    
    merged_df7_ov = merged_df7_ov[                                                
        (~merged_df7_ov['DocumentNo'].fillna('').str.startswith(('PSCM', 'JV'))) & 
        (~merged_df7_ov['JournalBatchName'].str.contains('EWT|WHT', na=False)) & 
        (~merged_df7_ov['BalAccountNo'].str.contains('EWT|WHT', na=False)) & 
        (merged_df7_ov['DocumentType'] == 'INVOICE')] 
                       
    # merged_df7_ov = merged_df7_ov.drop_duplicates().reset_index(drop=True)
    # merged_df7_ov.to_csv("debug_merged_df7_ov_filtering.csv", index=False)

    # Moving 'Posting Date' to the first column
    columns = ['PostingDate'] + [col for col in merged_df7_ov.columns if col != 'PostingDate']
    filtered_dfz = merged_df7_ov[columns]  

    filtered_sr_name = filtered_dfz.copy()
    filtered_sr_name = filtered_sr_name.groupby(['SCR_NAME']).agg({'Collected_Amount': 'sum'}).reset_index()
    filtered_sr_name = filtered_sr_name.rename(columns={'Collected_Amount': 'Total Collected Amount (PHP)'})
    filtered_sr_name['Total Collected Amount (PHP)'] = pd.to_numeric(filtered_sr_name['Total Collected Amount (PHP)'], errors='coerce')  
    filtered_sr_name['Total Collected Amount (PHP)'] = filtered_sr_name['Total Collected Amount (PHP)'].apply(lambda x: f"{x:,.2f}" if pd.notna(x) else None)                    
    filtered_sr_name = filtered_sr_name.sort_values(by='SCR_NAME')  
                                            
    # Assign the renamed DataFrame back to filtered_sr_name
    filtered_dfz = filtered_dfz.rename(columns={'DETAIL_ITEM_NAME': 'DETAIL_ITEM_NAME (Only 20 Chars Each Name)'})

    # filtered_dfz = merged_df7_ov[(merged_df7_ov['DetailDate'].apply(check_current))]
    target_totals = filtered_dfz['Collected_Amount'].sum()
    st.subheader(f"Target CURRENT of Collection [{asof_month}]  -  Total Collected Amount: PHP {target_totals:,.2f}")


    target_current_cnt = filtered_sr_name['SCR_NAME'].count() 
    st.markdown(f"#### CURRENT Target by CR Name ({target_current_cnt})")
    
    st.session_state.filtered_CURRENT = filtered_sr_name.copy()
    st.session_state.filtered_CURRENT_details = filtered_dfz.copy()

    col_cfg_sr = _numeric_column_config(filtered_sr_name)
    st.dataframe(filtered_sr_name, use_container_width=True, hide_index=True, key='current_sr_name', column_config=col_cfg_sr)
    csv = filtered_sr_name.to_csv(index=False)
    st.download_button(
        label="Download CURRENT Target by CR Name",
        data=csv,
        key='current_sr_name',
        file_name=f"Target CURRENT SCR_NAME of Collection [{asof_month}].csv",
        mime="text/csv")

    st.markdown("#### CURRENT Target with details")
    col_cfg_dfz = _numeric_column_config(filtered_dfz)
    st.dataframe(filtered_dfz, use_container_width=True, hide_index=True, column_config=col_cfg_dfz)                                                                                
    # gb = GridOptionsBuilder.from_dataframe(filtered_dfz)
    # gb.configure_default_column(
    #     editable=True,
    #     resizable=True,                    
    #     minWidth=100,
    #     filter=True,  # Enable filter on all columns
    #     filterParams={"type": "agTextColumnFilter"}  # Use text filter for flexibility
    # )
    # # gb.configure_selection(selection_mode="multiple", use_checkbox=True)
    # gb.configure_pagination(enabled=True, paginationAutoPageSize=True, paginationPageSize=10)
    # gb.configure_side_bar(filters_panel=True, columns_panel=False)

    # grid_options = gb.build()

    # # Render the grid
    # grid_response = AgGrid(  # noqa: F841
    #     filtered_dfz,
    #     gridOptions=grid_options,
    #     fit_columns_on_grid_load=False,
    #     theme="streamlit", # material, balham, alphine, streamlit
    #     key="3grids7",
    #     height=400,
    #     width="100%",
    #     update_mode="MANUAL"
    # )

    # # Optional: Force column auto-sizing after render
    # st.markdown("""
    #     <script>
    #         document.querySelector('ag-grid').api.autoSizeAllColumns();
    #     </script>
    #     """, unsafe_allow_html=True)
    #######################################################################################
    # result_df = pd.DataFrame(grid_response["data"])
    csv = filtered_dfz.to_csv(index=False)
    st.download_button(
        label="Download Report as CSV",
        data=csv,
        key="download_current_csv3",
        file_name=f"Target CURRENT of Collection [{asof_month}].csv",
        mime="text/csv" )           
    print("Done with CURRENT")  

@st.fragment
def CR_TARGET_COD_fragment():
    print("Processing COD")
    date_from_str = st.session_state.date_from_str  # noqa: F841
    date_to_str = st.session_state.date_to_str
    asof_month = (datetime.strptime(date_to_str, '%Y-%m-%d') + relativedelta(months=1,day=31)).strftime('%m-%Y').upper()   
    # df_overdue = st.session_state.display_df6s.copy()
    columns_to_include = ['Posting Date', 'Document No_', 'External Document No_', 'Customer No_', 'Closed by Entry No_', 'Entry No_', 'Journal Batch Name', 'Bal_ Account No_','Closed by Amount']
    df_overdue = st.session_state.result_df9[columns_to_include].copy()  
    
    # This filter is to prevent duplication, affected category COD | CONTRACT
    # df_overdue = df_overdue[~df_overdue['External Document No_'].str.contains('PR', na=False)]
    # df_overdue_2 = df_overdue[df_overdue['Journal Batch Name'] == 'PDC'].copy()      
    
    # df_overdue_2.to_csv("debug_df_overdue_2.csv", index=False)  # Debugging line to check the content of df_overdue_2                   
    df_overdue_2 = df_overdue[['Posting Date','Closed by Entry No_', 'Entry No_']].copy()  # noqa: F841
    display_df7 = st.session_state.result_df7.copy()      
    display_df7 = display_df7[~display_df7['ExternalDocumentNo'].str.contains('PR', na=False)]
    # Filter DF_Overdue (keep only non-zero, non-null values)
    # df_overdue = df_overdue.loc[
    #     (df_overdue['Entry No_'] != 0) & 
    #     (df_overdue['Entry No_'].notna())
    # ].copy()
    
    
    # -----------------------------------------------------------------------------------------------------------------------------------
    # Split and explode
    display_df7['AppliedCustLedgrNo'] = display_df7['AppliedCustLedgrNo'].str.strip().str.rstrip(";") # need to do split or explode    
    display_df7['AppliedCustLedgrNo'] = display_df7['AppliedCustLedgrNo'].str.split(' ; ')
    display_df7 = display_df7.explode('AppliedCustLedgrNo')
    display_df7 = display_df7.reset_index(drop=True)
    # display_df7.to_csv("debug_display_df7_explode.csv", index=False)
    # Then create a 2 new column to keep both values
    # Get the value from left befor '(' in display_df7['AppliedCustLedgrNo']
    display_df7['AppliedCustLedgrNo_Orig'] = display_df7['AppliedCustLedgrNo']
    display_df7['AppliedCustLedgrNo'] = display_df7['AppliedCustLedgrNo_Orig'].str.split('(').str[0].str.strip()
    # # Get the value from first '(' and last ')' in display_df7['AppliedCustLedgrNo_Orig']
    # display_df7['Applied_Ledgr_Status'] = display_df7['AppliedCustLedgrNo_Orig'].astype(str).str.extract(r'\((.*?)\)').fillna('').str.strip()
    # -----------------------------------------------------------------------------------------------------------------------------------
    
    
    # Ensure consistent data types
    # Clean key columns to avoid whitespace or type issues
    df_overdue['Closed by Entry No_'] = df_overdue['Closed by Entry No_'].astype(str).str.strip()
    df_overdue['Entry No_'] = df_overdue['Entry No_'].astype(str).str.strip()
    df_overdue['External Document No_'] = df_overdue['External Document No_'].astype(str).str.strip()
    display_df7['AppliedCustLedgrNo'] = display_df7['AppliedCustLedgrNo'].astype(str).str.strip()
    display_df7['ExternalDocumentNo'] = display_df7['ExternalDocumentNo'].astype(str).str.strip()
    
    # df_overdue.to_csv("debug_df_overdue.csv", index=False)  # Debugging line to check the content of df_overdue
    # display_df7.to_csv("debug_display_df7.csv", index=False)  # Debugging line to check the content of display_df7    
    # Previous: Perform the merge
    
    display_df7 = display_df7.merge(
        df_overdue,
        left_on=['AppliedCustLedgrNo','CustomerNo'],
        right_on=['Entry No_','Customer No_'],
        how='left')

    # display_df7.to_csv("debug_display_df7_final_1.csv", index=False)
    

# #>>>_________________________________________________________________________________________________________________________________________#     
#     # STEP 2 mapping
#     # Fill null values in display_df7 using Closed by Entry No_ matches 
#     # Put additional here where [BalAccountNo] <> 'WHT' and 'EWT' before processing the mapping
#     closed_by_column = 'Closed by Entry No_'
#     doc_no_column = 'Document No_'
#     if closed_by_column in df_overdue.columns and doc_no_column in df_overdue.columns:
#         # Filter df_overdue to ensure valid keys
#         df_overdue_clean = df_overdue.loc[
#             (df_overdue[closed_by_column].notna()) & 
#             (df_overdue[closed_by_column] != 0) & 
#             (df_overdue[closed_by_column].astype(str).str.strip() != '') &
#             (df_overdue[doc_no_column].notna()) &
#             (df_overdue[doc_no_column].astype(str).str.strip() != '')
#         ].copy()
        
#         # Create composite key in df_overdue_clean
#         df_overdue_clean['composite_key'] = df_overdue_clean[closed_by_column].astype(str) + '|' + df_overdue_clean[doc_no_column].astype(str)
        
#         # Create mappings for each column using composite key
#         mappings = {}
#         for col in columns_to_include:
#             if col in df_overdue_clean.columns:
#                 if col == closed_by_column:
#                     # Map Closed by Entry No_ to itself
#                     mappings[col] = dict(zip(df_overdue_clean['composite_key'], df_overdue_clean[closed_by_column]))
#                 elif col == doc_no_column:
#                     # Map Document No_ to itself
#                     mappings[col] = dict(zip(df_overdue_clean['composite_key'], df_overdue_clean[doc_no_column]))
#                 else:
#                     # Map other columns using composite key
#                     mappings[col] = dict(zip(df_overdue_clean['composite_key'], df_overdue_clean[col]))
        
#         # Create composite key in display_df7
#         display_df7['composite_key'] = display_df7['AppliedCustLedgrNo'].astype(str) + '|' + display_df7['DocumentNo'].astype(str)
        
#         # Apply mappings to fill nulls in display_df7
#         for col in columns_to_include:
#             if col in display_df7.columns and col in mappings:
#                 display_df7[col] = display_df7[col].fillna(display_df7['composite_key'].map(mappings[col]))
        
#         # Clean up by removing composite_key column from display_df7
#         display_df7 = display_df7.drop('composite_key', axis=1)
    

#     df_overdue.to_csv("debug_df_overdue_final.csv", index=False)    
    
#     # Save the result
#     display_df7.to_csv("debug_display_df7_final_2_ENvsCEN.csv", index=False)
 
# #_________________________________________________________________________________________________________________________________________<<<#                    
    
    # Debugging: Check overall null counts
    print("Null counts after processing (entire dataframe):")
    print(display_df7[['Posting Date', 'Document No_', 'Closed by Entry No_', 'Entry No_']].isna().sum())
    print()
                
    
    # asof_month = (datetime.strptime(date_to_str, '%Y-%m-%d') + relativedelta(months=1,day=31))
    asof_month = (datetime.strptime(date_to_str, '%Y-%m-%d') + relativedelta(months=1)).replace(day=1)
    asof_month_str = asof_month.strftime('%m-%Y').upper()
    # THESE ARE THE PREVIOUS CONDITION, WHERE PSI-170425 DOESN'T APPEAR ... this code is to trace the orignal Posting Date from BC365 Customer Ledger Entry
    # display_df7['Posting Date'] = pd.to_datetime(display_df7['Posting Date'], format='%b-%Y', errors='coerce').dt.strftime('%b-%Y').str.upper()
    # display_df7['Posting Date'] = pd.to_datetime(display_df7['PostingDate'], format='%m-%Y', errors='coerce').dt.strftime('%m-%Y')
    # display_df7['Posting Date'] = pd.to_datetime(display_df7['PostingDate'], format='%m-%Y', errors='coerce').dt.strftime('%m-%Y')
    # display_df7['DueDate_no'] = pd.to_datetime(display_df7['DueDate'], format='%m-%Y', errors='coerce').dt.strftime('%m-%Y') 
    
    # THIS IS THE REVISE CONDITION, WHERE PSI-170425 APPEAR 
    display_df7['Posting Date'] = pd.to_datetime(display_df7['Posting Date'], format='%m-%Y', errors='coerce')
    display_df7['DueDate_no'] = pd.to_datetime(display_df7['DueDate'], format='%m-%Y', errors='coerce')   
    
    # Save the result
    # display_df7.to_csv(f"debug_display_df7_final_PostingDate_{asof_month_str}.csv", index=False)
    
    # display_df7 = display_df7[(display_df7['Posting Date'] == asof_month)]
    display_df7 = display_df7[
            (display_df7['Posting Date'].dt.year == asof_month.year) & (display_df7['Posting Date'].dt.month == asof_month.month)
        ].copy()
    
    # display_df7.to_csv(f"debug_display_df7_final_PostingDate_{asof_month_str}_final.csv", index=False) 
                                                               
    display_df7['CollectedAmount'] = pd.to_numeric(display_df7['CollectedAmount'], errors='coerce')   
    
    display_df7.rename(columns={
            'CollectedAmount': 'Collected_Amount',
            'PaidUnpaid': 'Remaining Balance'}, inplace=True)   
    # Ensure expected columns exist for category tagging (CO_Conditions expects SR2/SR_Code2)
    if 'SR2' not in display_df7.columns and 'SCR_NAME' in display_df7.columns:
        display_df7['SR2'] = display_df7['SCR_NAME']
    if 'SR_Code2' not in display_df7.columns and 'SCR' in display_df7.columns:
        display_df7['SR_Code2'] = display_df7['SCR']
    if 'Balance Due' not in display_df7.columns and 'Remaining Balance' in display_df7.columns:
        display_df7['Balance Due'] = display_df7['Remaining Balance']
    if 'Payment_Terms' not in display_df7.columns and 'PaymentTermsCode' in display_df7.columns:
        display_df7['Payment_Terms'] = display_df7['PaymentTermsCode']

    # Apply category logic to add Category and DSS2_Name columns
    display_df7 = apply_category_to_display_df(display_df7)
    
    ##
    ## display_df7.to_csv("debug_display_df7_final1.csv", index=False)                          
    ## filtered_df = display_df7[(display_df7['Remaining Balance'] == 0) & (display_df7['DetailDate'].apply(same_month))]
    ## filtered_df = display_df7[(display_df7['Remaining Balance'] == 0) & (display_df7['DetailDate'].apply(same_month)) & (~display_df7['DocumentNo'].str.startswith(('PSCM', 'JV'))) & (~display_df7['DetailDoc'].str.contains(('PSCM', 'JV'))) & (display_df7['DocumentType'] == 'INVOICE')] #
    ## (display_df7['Remaining Balance'] == 0) was remove then put column Payment Terms Column
    ## | (display_df7['DueDate_no'] > asof_month)
    ## (display_df7['Remaining Balance'] == 0) |
    ##
    
    display_df7["logic"] = display_df7['DueDate_no'] >= display_df7['Posting Date']
    display_df7["logic_behind"] = display_df7['DueDate_no'].astype(str) + " | " + display_df7['Posting Date'].astype(str)
    # display_df7.to_csv("debug_filtered_df_initial.csv", index=False)  # OK
    
    # filtered_df = display_df7.copy()
    # filtered_df = display_df7[
    #         (display_df7['DueDate'] >= display_df7['Posting Date'])].copy() 
    # (display_df7['DetailDate'].apply(same_month1)) is removed because it is not used in the filtered_df new conditions Posting Date == Document Date

    # Compare Posting Date and DocumentDate to ensure they are in the same month
    if 'DocumentDate' in display_df7.columns:
        # Ensure DocumentDate is datetime for comparison
        display_df7['DocumentDate'] = pd.to_datetime(display_df7['DocumentDate'], errors='coerce')
        
        # Compare year and month, handling NaN values properly
        # If either date is NaN, the result should be False (like same_month1 returns False on errors)
        same_month_condition = (
            display_df7['Posting Date'].dt.year == display_df7['DocumentDate'].dt.year
        ) & (
            display_df7['Posting Date'].dt.month == display_df7['DocumentDate'].dt.month
        ) & (
            display_df7['Posting Date'].notna() & display_df7['DocumentDate'].notna()
        )
    else:
        # If DocumentDate column doesn't exist, exclude all rows (False)
        # This ensures we only include rows where we can verify the same month condition
        same_month_condition = False
    #(display_df7['DetailDate'].apply(same_month1)) & 
    filtered_df = display_df7[
                ((display_df7['Remaining Balance'] == 0) | (display_df7['DueDate'] > display_df7['Posting Date'])) &                 
                same_month_condition & # Month and Year of [Posting Date] == [DocumentDate]
                (~display_df7['DocumentNo'].fillna('').str.startswith(('PSCM', 'JV'))) & 
                (~display_df7['DetailDoc'].str.contains('PSCM|JV', na=False)) &   #### ---->>> Need more review on this one because it may remove some legit collection row if with partial collection then return "CM" or "JV"
                (~display_df7['JournalBatchName'].str.contains('EWT|WHT', na=False)) & 
                (~display_df7['BalAccountNo'].str.contains('EWT|WHT', na=False)) & 
                (display_df7['DocumentType'] == 'INVOICE')].copy()
    
    # filtered_df.to_csv("debug_filtered_df_after_filter1.csv", index=False) 
    
    filtered_df = filtered_df[( ~filtered_df['AppliedCustLedgrNo_Orig'].str.contains('EWT|WHT', na=False) )]
    
    # filtered_df.to_csv("debug_filtered_df_after_filter2.csv", index=False)  

    ## >>> DROPPING DUPLICATES
    columns = ['EntryNo','PostingDate','DueDate','DocumentNo','DocumentType','Gross Amount','CustomerNo',
               'ExternalDocumentNo','CustomerName','PaymentTermsCode','Remaining Balance','DetailDoc','DetailDate',
               'DetailAmount','Collected_Amount','Collected_EWT']
    filtered_df = filtered_df.sort_values(by=["Closed by Entry No_"], ascending=[False])
    filtered_df = filtered_df.drop_duplicates(subset=columns, keep='first').reset_index(drop=True)  
    # filtered_df.to_csv("debug_filtered_df_final_Drop.csv", index=False)
    ## <<< DROPPING DUPLICATES
    
    
    # filtered_df.to_csv("debug_filtered_df_final.csv", index=False)     
    filtered_df['Closed by Entry No_'] = pd.to_numeric(filtered_df['Closed by Entry No_'], errors='coerce')
    filtered_df['Entry No_'] = pd.to_numeric(filtered_df['Entry No_'], errors='coerce')
    
    filtered_sr_name_cd = filtered_df.copy()
    filtered_sr_name_cd = filtered_sr_name_cd.groupby(['SCR_NAME']).agg({'Collected_Amount': 'sum'}).reset_index()
    
    # filtered_sr_name_cd.to_csv("debug_filtered_sr_name_cd_groupby.csv", index=False)   
    
    filtered_sr_name_cd = filtered_sr_name_cd.rename(columns={'Collected_Amount': 'Total Collected Amount (PHP)'})
    filtered_sr_name_cd['Total Collected Amount (PHP)'] = pd.to_numeric(filtered_sr_name_cd['Total Collected Amount (PHP)'], errors='coerce')  
    filtered_sr_name_cd['Total Collected Amount (PHP)'] = filtered_sr_name_cd['Total Collected Amount (PHP)'].apply(lambda x: f"{x:,.2f}" if pd.notna(x) else None)                    
    print("Sorting")
    filtered_sr_name_cd = filtered_sr_name_cd.sort_values(by='SCR_NAME')     

    print("processing target total")
    target_total = filtered_df['Collected_Amount'].sum()
    st.subheader(f"Target COD of Collection [{asof_month_str}] -  Total Collected Amount: PHP {target_total:,.2f}")
    
    target_current_cnt = filtered_sr_name_cd['SCR_NAME'].count() 
    st.markdown(f"#### COD Target by CR Name ({target_current_cnt})")
    
    st.session_state.filtered_COD = filtered_sr_name_cd.copy()
    st.session_state.filtered_COD_details = filtered_df.copy()
    
    col_cfg_cod = _numeric_column_config(filtered_sr_name_cd)
    st.dataframe(filtered_sr_name_cd, use_container_width=True, hide_index=True, key='cod_sr_name', column_config=col_cfg_cod)
    csv = filtered_sr_name_cd.to_csv(index=False)
    st.download_button(
        label="Download COD Target by CR Name",
        data=csv,
        key='cod_sr_name',
        file_name=f"Target COD SCR_NAME of Collection [{asof_month_str}].csv",
        mime="text/csv")
    
    st.markdown("#### COD Target with details") 
    print()
    print("processing dataframe view")
    col_cfg_cod_df = _numeric_column_config(filtered_df)
    st.dataframe(filtered_df, use_container_width=True, hide_index=True, column_config=col_cfg_cod_df) 
    print("processing dataframe view done")                                                                                                  
    print()
    # gb = GridOptionsBuilder.from_dataframe(filtered_df)
    # gb.configure_default_column(
    #     editable=True,
    #     resizable=True,                        
    #     minWidth=100,
    #     filter=True,  # Enable filter on all columns
    #     filterParams={"type": "agTextColumnFilter"}  # Use text filter for flexibility
    # )
    # # gb.configure_selection(selection_mode="multiple", use_checkbox=True)
    # gb.configure_pagination(enabled=True, paginationAutoPageSize=True, paginationPageSize=10)
    # gb.configure_side_bar(filters_panel=True, columns_panel=False)
    
    # grid_options = gb.build()
    
    # # Render the grid
    # grid_response = AgGrid(  # noqa: F841
    #     filtered_df,
    #     gridOptions=grid_options,
    #     fit_columns_on_grid_load=False,
    #     theme="streamlit", # material, balham, alphine, streamlit
    #     key="grids7",
    #     height=400,
    #     width="100%",
    #     update_mode="MANUAL"
    # )

    # # Optional: Force column auto-sizing after render
    # st.markdown("""
    #     <script>
    #         document.querySelector('ag-grid').api.autoSizeAllColumns();
    #     </script>
    #     """, unsafe_allow_html=True)
    #######################################################################################
    # result_df = pd.DataFrame(grid_response["data"])
    csv = filtered_df.to_csv(index=False)
    st.download_button(
        label="Download Report as CSV",
        data=csv,
        key="download_current_csv2",
        file_name=f"Target COD of Collection [{asof_month_str}].csv",
        mime="text/csv"
    )        
    print("Done with COD")            

@st.fragment
def CR_TARGET_RETURNS_fragment():
    
    # date_from_str = st.session_state.date_from_str
    date_to_str = st.session_state.date_to_str
    asof_month = (datetime.strptime(date_to_str, '%Y-%m-%d') + relativedelta(months=1,day=31)).strftime('%m-%Y').upper() 
    with st.spinner("Target RETURNS - Processing..."):
        # Ensure result_df7 is available in session state SCR_NAME
        if 'result_df7' not in st.session_state:
            st.error("Error: result_df7 not found in session state. Please ensure data is loaded.")
        else:
            df_returns = st.session_state.result_df7.copy()
            # df_returns.to_csv("debug_df_returns_initial_full.csv", index=False)  # Debugging line to check the content of df_returns
        
        
        #Add to st.dataframe (#'Customer No_', 'Posting Date',	'Due Date',	'Name',	'Document No_', 'External Document No_', 'Balance Due')                        
        df_returns = df_returns[['PostingDate', 'DueDate','DocumentDate', 'SCR_NAME', 'CustomerNo',  'CustomerName', 'DocumentNo', 'ExternalDocumentNo','Remaining Balance', 'CollectedAmount','Collected_Return','DetailDoc','DetailDate','DetailAmount','AppliedCustLedgrNo']]
        # df_returns condition where 'Document No_' = 'PSCM' or ('Document No_' startswith 'OBC' and 'External Document No_' contains 'CM' and 'RRM')
        df_returns = df_returns[
            ((df_returns['DocumentNo'].str.startswith('PSCM'))  | 
            (df_returns['ExternalDocumentNo'].str.contains('CM|RRM|MDC',case=False,na=False))) & 
            (~df_returns['DocumentNo'].str.contains('JV',case=False,na=False))
        ].copy()
        # df_returns.to_csv("debug_df_returns_initial_filter.csv", index=False)
        
        ############################################################################
        # THIS IS THE REVISE CONDITION, FOR RUD.
        df_returns['PostingDate_no'] = pd.to_datetime(df_returns['PostingDate'], format='%m-%Y', errors='coerce')
        df_returns['DocumentDate_no'] = pd.to_datetime(df_returns['DocumentDate'], format='%m-%Y', errors='coerce')                   
        # Filter Posting Date is not in the same month and year as Document Date to eliminate RUD
        df_returns = df_returns[
            (df_returns['DocumentNo'].str.startswith('OBC')) |  # Keep all rows starting with 'OBC'
             # For non-'OBC' rows, apply date condition
            ((~df_returns['DocumentNo'].str.startswith('OBC')) & 
            ((df_returns['PostingDate_no'].dt.year != df_returns['DocumentDate_no'].dt.year) | 
             (df_returns['PostingDate_no'].dt.month != df_returns['DocumentDate_no'].dt.month)
            ))
        ].copy()  
        
        # Dropping the columns
        df_returns = df_returns.drop(columns=['PostingDate_no','DocumentDate_no'], errors='ignore')  
        ############################################################################
        
        
        # df_returns.to_csv("debug_df_returns_after_filter.csv", index=False)  # Debugging line to check the content of df_returns
        
       
        # Group by SCR_NAME and sum 'Balance Due' only in column and post to new st.dataframe()
        df_returns['Collected_Return'] = pd.to_numeric(df_returns['Collected_Return'], errors='coerce').fillna(0)
        df_returns_groupby = df_returns.groupby(['SCR_NAME' ], as_index=False).agg({'Collected_Return': 'sum'})

        df_returns_groupby = df_returns_groupby.sort_values(by='SCR_NAME', ascending=True)
        df_returns_groupby = df_returns_groupby.reset_index(drop=True)
        
        # df_returns_groupby.to_csv("debug_df_returns_groupby.csv", index=False)  # Debugging line to check the content of df_returns_groupby
        
        st.markdown("#### Returns for Target - Total Amount by SCR Name")
        
        st.session_state.filtered_RETURNS_GROUPBY = df_returns_groupby.copy()

        col_cfg_ret = _numeric_column_config(df_returns_groupby)
        st.dataframe(df_returns_groupby, use_container_width=True, hide_index=True, column_config=col_cfg_ret)
        # Button to save the DataFrame as CSV   
        csv = df_returns_groupby.to_csv(index=False)
        st.download_button(
            label="Download Returns for Target by SCR Name",
            data=csv,
            key='returns_target_groupby',
            file_name=f"Returns in Target by SCR Name for the month of [{asof_month}].csv",
            mime="text/csv"
        )

        df_returns['Collected_Return'] = pd.to_numeric(df_returns['Collected_Return'], errors='coerce').fillna(0)
        total_balance_due = df_returns['Collected_Return'].sum()
        df_returns = df_returns.sort_values(by='PostingDate', ascending=False)
        df_returns = df_returns.reset_index(drop=True)
        
        #Add total balance due                                                
        st.markdown("#### Returns for Target - Total Balance Due: PHP {:,.2f}".format(total_balance_due))
        # st.session_state.filtered_RETURNS = df_returns.copy()
        col_cfg_ret2 = _numeric_column_config(df_returns)
        st.dataframe(df_returns, use_container_width=True, hide_index=True, column_config=col_cfg_ret2)
        # Button to save the DataFrame as CSV
        csv = df_returns.to_csv(index=False)
        st.download_button(
            label="Download Returns for Target",
            data=csv,
            key='returns_target',
            file_name=f"Returns in Target for the month of [{asof_month}].csv",
            mime="text/csv"
        )
        print("Done with RETURNS")    
            
@st.fragment
def CR_TARGET_OADJ_GL_fragment():
    date_to_str = st.session_state.date_to_str
    asof_month = (datetime.strptime(date_to_str, '%Y-%m-%d') + relativedelta(months=1,day=31)).strftime('%m-%Y').upper() 
    if 'result_df10' not in st.session_state:
        st.error("Error: result_df10 not found in session state. Please ensure data is loaded.")
    else:
        df_adjustments = st.session_state.result_df10.copy()
        df_adjustments['Posting Date'] = pd.to_datetime(df_adjustments['Posting Date'], errors='coerce').dt.strftime('%m/%d/%Y')
        df_adjustments['Document Date'] = pd.to_datetime(df_adjustments['Document Date'], errors='coerce').dt.strftime('%m/%d/%Y')
        
    df_adjustments = df_adjustments[
        (df_adjustments['Document No_'].str.startswith('JV', na=False)) | 
        (df_adjustments['External Document No_'].str.contains('JV|DM|Adj',case=False,na=False)) |
        (df_adjustments['Bal_ Account No_'].str.contains('DISC|SC|WHT',case=False,na=False)) |
        (df_adjustments['Bal_ Account No_'].isna()) | (df_adjustments['Bal_ Account No_'].str.strip().eq('')) |
        (df_adjustments['Description'].str.contains('EWT|WHT|Adj',case=False,na=False))
    ].copy()
    df_adjustments = df_adjustments.sort_values(by='Document No_', ascending=True)
    df_adjustments['Amount'] = df_adjustments['Amount'].apply(lambda x: f"{x:,.2f}" if pd.notna(x) else None)
    
    gb = GridOptionsBuilder.from_dataframe(df_adjustments)
    gb.configure_default_column(filter=True, sortable=True, resizable=True)
    gb.configure_column('Document No_', filter='agSetColumnFilter')  # Dropdown with unique values
    grid_options = gb.build()

    # Display the grid
    AgGrid(df_adjustments, gridOptions=grid_options, height=400, fit_columns_on_grid_load=True, key='adjustments_agGrid')
    
    # Button to save the DataFrame as CSV
    csv = df_adjustments.to_csv(index=False)
    st.download_button(
        label="Download Other Adjustments for Target",
        data=csv,            
        key='adjustment_target_OADJ_GL',
        file_name=f"Other Adjustments for Target AsOf AR [{asof_month}].csv",
        mime="text/csv"
    )  
    
    # Save the filtered dataframe to session state for later use
    st.session_state.ADJUSTMENTS_GL = df_adjustments.copy()
                                                 
    print("Done with ADJUSTMENTS - GL")  
    
@st.fragment
def CR_TARGET_OADJ_DISC_fragment():
    if 'ADJUSTMENTS_GL' not in st.session_state:
        st.error("Error: result_df10 not found in session state. Please ensure data is loaded.")
    else:   
        df_adjustments_DISC = st.session_state.ADJUSTMENTS_GL.copy()
        df_adjustments_DISC = df_adjustments_DISC[
            (df_adjustments_DISC['Bal_ Account No_'].str.contains('DISC|SC',case=False,na=False))       
        ].copy()
        df_adjustments_DISC = df_adjustments_DISC.sort_values(by='Document No_', ascending=True)
        # Convert 'Amount' to numeric, coercing errors to NaN
        df_adjustments_DISC['Amount'] = pd.to_numeric(df_adjustments_DISC['Amount'], errors='coerce')

        # Apply formatting to numeric values, keeping NaN/None as None
        df_adjustments_DISC['Amount'] = df_adjustments_DISC['Amount'].apply(lambda x: f"{x:,.2f}" if pd.notna(x) else None)
        
        
        # Display the DataFrame
        st.markdown("#### Other Adjustments for Target - Discount / Sales Credit")
        # st.session_state.filtered_ADJUSTMENTS_DISC = df_adjustments_DISC.copy()
        col_cfg_disc = _numeric_column_config(df_adjustments_DISC)
        st.dataframe(df_adjustments_DISC, use_container_width=True, hide_index=True, column_config=col_cfg_disc)
        # Button to save the DataFrame as CSV
        csv = df_adjustments_DISC.to_csv(index=False)   
        st.download_button(
            label="Download Other Adjustments for Target - Discount / Sales Credit",
            data=csv,
            key='adjustment_target_OADJ_DISC',
            file_name="Other Adjustments for Target - Discount Sales Credit AsOf AR.csv",
            mime="text/csv"
        )
        print("Done with ADJUSTMENTS - DISC")

@st.fragment
def CR_TARGET_OADJ_TAX_fragment():
    if 'ADJUSTMENTS_GL' not in st.session_state:
        st.error("Error: result_df10 not found in session state. Please ensure data is loaded.")
    else:   
        df_adjustments_TAX = st.session_state.ADJUSTMENTS_GL.copy()
        
        # Convert 'Document Type' to string, handling NaN appropriately
        df_adjustments_TAX['Document Type'] = df_adjustments_TAX['Document Type'].astype(str).replace('nan', '')
        
        df_adjustments_TAX = df_adjustments_TAX[
            (df_adjustments_TAX['Bal_ Account No_'].str.contains('WHT|EWT',case=False,na=False)) |
            # or equal to null or empty
            (
             (df_adjustments_TAX['Bal_ Account No_'].isna()) | (df_adjustments_TAX['Bal_ Account No_'].str.strip().eq('')) & 
             ((df_adjustments_TAX['Description'].str.contains('EWT|WHT',case=False,na=False)) |
             # and Document No_ conatains 'JV' and 'EWT' or 'WHT'
             (df_adjustments_TAX['Document No_'].str.contains('JV|EWT|WHT',case=False,na=False))) &
             # and document type is blank
             (df_adjustments_TAX['Document Type'].isna() | df_adjustments_TAX['Document Type'].str.strip().eq(''))   
            )       
        ].copy()
        
        df_adjustments_TAX = df_adjustments_TAX.sort_values(by='Document No_', ascending=True)
        # Convert 'Amount' to numeric, coercing errors to NaN
        df_adjustments_TAX['Amount'] = pd.to_numeric(df_adjustments_TAX['Amount'], errors='coerce')        
        df_adjustments_TAX['Amount'] = df_adjustments_TAX['Amount'].apply(lambda x : f"{x:,.2f}" if pd.notna(x) else None)
        
        # Display the DataFrame
        st.markdown("#### Other Adjustments for Target - EWT / WHT")
        # st.session_state.filtered_ADJUSTMENTS_TAX = df_adjustments_TAX.copy()
        col_cfg_tax = _numeric_column_config(df_adjustments_TAX)
        st.dataframe(df_adjustments_TAX, use_container_width=True, hide_index=True, column_config=col_cfg_tax)
        # Button to save the DataFrame as CSV
        csv = df_adjustments_TAX.to_csv(index=False)   
        st.download_button(
            label="Download Other Adjustments for Target - EWT / WHT",
            data=csv,
            key='adjustment_target_OADJ_TAX',
            file_name="Other Adjustments for Target - EWT WHT AsOf AR.csv",
            mime="text/csv"
        )
        print("Done with ADJUSTMENTS - TAX")
        
@st.fragment
def CR_TARGET_OADJ_OTH_fragment():
    if 'ADJUSTMENTS_GL' not in st.session_state:
        st.error("Error: result_df10 not found in session state. Please ensure data is loaded.")
    else:   
        df_adjustments_OTH = st.session_state.ADJUSTMENTS_GL.copy()
        df_adjustments_OTH = df_adjustments_OTH[
            ((df_adjustments_OTH['Description'].str.contains(r'\(DM', case=False, na=False)) | 
            (df_adjustments_OTH['Description'].str.contains('DM#', case=False, na=False))) |
            ~(df_adjustments_OTH['Description'].str.contains('EWT|WHT',case=False,na=False)) |
            ~(df_adjustments_OTH['Document No_'].str.contains('JV|EWT|WHT',case=False,na=False)) |
            ~(df_adjustments_OTH['Bal_ Account No_'].str.contains('DISC|SC',case=False,na=False))
        ].copy()
        df_adjustments_OTH = df_adjustments_OTH.sort_values(by='Document No_', ascending=True)
        df_adjustments_OTH['Amount'] = pd.to_numeric(df_adjustments_OTH['Amount'], errors='coerce')

        # Display the DataFrame
        st.markdown("#### Other Adjustments for Target - Others")
        # st.session_state.filtered_ADJUSTMENTS_OTH = df_adjustments_OTH.copy()
        col_cfg_oth = _numeric_column_config(df_adjustments_OTH)
        st.dataframe(df_adjustments_OTH, use_container_width=True, hide_index=True, column_config=col_cfg_oth)
        # Button to save the DataFrame as CSV
        csv = df_adjustments_OTH.to_csv(index=False)   
        st.download_button(
            label="Download Other Adjustments for Target - Others",
            data=csv,
            key='adjustment_target_OADJ_OTH',
            file_name="Other Adjustments for Target - Others AsOf AR.csv",
            mime="text/csv"
        )
        print("Done with ADJUSTMENTS - OTHERS")

@st.fragment    
def CR_TARGET_OADJ_fragment():
    
    date_from_str = st.session_state.date_from_str 
    date_to_str = st.session_state.date_to_str
    asof_month = (datetime.strptime(date_to_str, '%Y-%m-%d') + relativedelta(months=1,day=31)).strftime('%m-%Y').upper() 
    if 'result_df6' not in st.session_state:
        st.error("Error: result_df6 not found in session state. Please ensure data is loaded.")
    else:
        df_adjustments = st.session_state.display_df6s.copy()
    
    # Add to st.dataframe (#'Customer No_', 'Posting Date',	'Due Date',	'Name',	'Document No_', 'External Document No_', 'Balance Due')                        
    df_adjustments = df_adjustments[['CR_NAME', 'Customer No_', 'Posting Date', 'Due Date', 'Name', 'Document No_', 'External Document No_','Description', 'Balance Due']]
    # df_adjustments condition where 'Document No_' = 'PSCM' or ('Document No_' startswith 'OBC' and 'External Document No_' contains 'CM' and 'RRM')
    df_adjustments = df_adjustments[
        (df_adjustments['Document No_'].str.startswith('JV')) | 
        (df_adjustments['External Document No_'].str.contains('JV|DM|Adj',case=False,na=False)) |
        (df_adjustments['Description'].str.contains('EWT|WHT|Adj',case=False,na=False))
    ].copy()
    
    # Group by CR_NAME and sum 'Balance Due' only in column and post to new st.dataframe()
    df_adjustments_groupby = df_adjustments.groupby(['CR_NAME'], as_index=False).agg({'Balance Due': 'sum'})
    df_adjustments_groupby['Balance Due'] = pd.to_numeric(df_adjustments_groupby['Balance Due'], errors='coerce').fillna(0)
    df_adjustments_groupby = df_adjustments_groupby.sort_values(by='CR_NAME', ascending=True)
    df_adjustments_groupby = df_adjustments_groupby.reset_index(drop=True)
    st.markdown("#### Other Adjustments for Target - Balance Due by SR Name")
    st.session_state.filtered_ADJUSTMENTS_GROUPBY = df_adjustments_groupby.copy()
    col_cfg_adj_grp = _numeric_column_config(df_adjustments_groupby)
    st.dataframe(df_adjustments_groupby, use_container_width=True, hide_index=True, column_config=col_cfg_adj_grp)
    # Button to save the DataFrame as CSV
    csv = df_adjustments_groupby.to_csv(index=False)
    st.download_button(
        label="Download Other Adjustments for Target by CR Name",
        data=csv,
        key='adjustments_target_groupby',
        file_name=f"Other Adjustments for Target by CR Name AsOf AR [{date_from_str}].csv",
        mime="text/csv"
    )
                            
    df_adjustments['Balance Due'] = pd.to_numeric(df_adjustments['Balance Due'], errors='coerce').fillna(0)
    total_balance_due2 = df_adjustments['Balance Due'].sum()
    df_adjustments = df_adjustments.sort_values(by='Posting Date', ascending=False)
    df_adjustments = df_adjustments.reset_index(drop=True)

    # Add total balance due
    st.markdown("#### Other Adjustments for Target - Total Balance Due: PHP {:,.2f}".format(total_balance_due2))
    # st.session_state.filtered_ADJUSTMENTS = df_adjustments.copy()
    col_cfg_adj = _numeric_column_config(df_adjustments)
    st.dataframe(df_adjustments, use_container_width=True, hide_index=True, column_config=col_cfg_adj)
    # Button to save the DataFrame as CSV
    csv = df_adjustments.to_csv(index=False)
    st.download_button(
        label="Download Other Adjustments for Target",
        data=csv,
        key='adjustment_target',
        file_name=f"Other Adjustments for Target AsOf AR [{asof_month}].csv",
        mime="text/csv"
    )                                               
    print("Done with ADJUSTMENTS")  

@st.fragment
def DS_btn_1_fragment():
    ###
    result_df = st.session_state.result_df
    date_from_str = st.session_state.date_from_str
    date_to_str = st.session_state.date_to_str
    cols = st.columns(2) # Make buttons side by side
    with cols[0]:
        csv = result_df.to_csv(index=False)
        st.download_button(
            label="Download Report as CSV",
            data=csv,
            file_name=f"Collection Report {date_from_str} - {date_to_str}.csv",
            mime="text/csv")

@st.fragment
def TOverdue_fragment():
    ###
    st.markdown("#### Overdue Summary")                        
    result_df = st.session_state.result_df
    date_from_str = st.session_state.date_from_str
    date_to_str = st.session_state.date_to_str
    
    # Reuse DSS filter from tab1
    st.markdown("##### Filter by DSS")
    dss_summary = result_df.groupby(['dss_name', 'dept', 'dept_code']).agg({'remaining_balance': 'sum'}).reset_index()
    dss_summary = dss_summary[dss_summary['remaining_balance'] > 0].groupby(['dss_name', 'dept_code']).agg({
        'remaining_balance': 'sum',
        'dept': 'first'
    }).reset_index()
    dss_summary = dss_summary.sort_values(by=['dss_name','dept_code'])
    
    dss_options = ['All'] + [f"{row['dss_name']} - (Remaining Balance: PHP {row['remaining_balance']:,.2f}, Dept: {row['dept_code']})" 
                        for _, row in dss_summary.iterrows()]
    
    selected_dss = st.selectbox("Select DSS", dss_options, key="dss_dashboard_selectbox")
    
    selected_dss_value = selected_dss if selected_dss == 'All' else selected_dss.split(' - (')[0]
    selected_dept_value = None if selected_dss == 'All' else selected_dss.split('Dept: ')[1].rstrip(')')
    
    if selected_dss_value == 'All':
        filtered_df = result_df[result_df['remaining_balance'].notna() & (result_df['remaining_balance'] != 0)]
    else:
        filtered_df = result_df[(result_df['dss_name'] == selected_dss_value) & 
                            (result_df['dept_code'].astype(str) == selected_dept_value) &
                            result_df['remaining_balance'].notna() & 
                            (result_df['remaining_balance'] != 0)]
    
    # Overdue Report
    st.markdown("##### Overdue Report by CR and Customer")
    if 'days_overdue' in filtered_df.columns:
        overdue_df = filtered_df[(filtered_df['days_overdue'] > 0) & (filtered_df['remaining_balance'] > 0)]
        if not overdue_df.empty:
            overdue_report = overdue_df.groupby(['scr_name', 'customer_name']).agg({
                'remaining_balance': 'sum',
                'days_overdue': 'mean'
            }).reset_index()
            overdue_report = overdue_report.rename(columns={
                'remaining_balance': 'Overdue Amount (PHP)',
                'days_overdue': 'Average Days Overdue'
            })
            overdue_report = overdue_report.sort_values(by='Overdue Amount (PHP)', ascending=False)
            
            display_overdue_df = overdue_report.copy()

            col_cfg_overdue = _numeric_column_config(display_overdue_df)
            st.dataframe(display_overdue_df, use_container_width=True, hide_index=True, column_config=col_cfg_overdue)

            overdue_csv = overdue_report.to_csv(index=False)
            st.download_button(
                label="Download Overdue Report",
                data=overdue_csv,
                file_name=f"Overdue_Report_dss_{selected_dss_value}_{date_from_str}_to_{date_to_str}.csv",
                mime="text/csv"
            )
            
            # Create two columns for side-by-side pie charts
            cols1, cols2 = st.columns(2)
            with cols1:
                # Bar Chart: Overdue Amount by CR
                st.markdown("##### Overdue Amount by CR")
                fig_bar = px.bar(
                    overdue_report,
                    x='scr_name',
                    y='Overdue Amount (PHP)',
                    title='Overdue Amount by CR',
                    color='Overdue Amount (PHP)',
                    color_continuous_scale='Reds',
                    text_auto='.2s'
                )
                fig_bar.update_layout(
                    xaxis_title="CR",
                    yaxis_title="Overdue Amount (PHP)",
                    xaxis_tickangle=45
                )
                st.plotly_chart(fig_bar, use_container_width=True, key="customer_overdue_amount_chart")
            
            with cols2:   
                # Bar Chart: Customer with Overdue Days
                if 'days_overdue' in filtered_df.columns and not overdue_df.empty:
                    company_overdue = overdue_df.groupby('customer_name').agg({
                        'days_overdue': 'mean'
                    }).reset_index()
                    company_overdue = company_overdue.sort_values(by='days_overdue', ascending=False)
                    
                    st.markdown("##### Customer with Overdue Days")
                    fig_bar_overdue_days_2 = px.bar(
                        company_overdue,
                        x='customer_name',
                        y='days_overdue',
                        title='Customer by Average Overdue Days',
                        color='days_overdue',
                        color_continuous_scale='Reds',
                        text='days_overdue',
                        height=500
                    )
                    fig_bar_overdue_days_2.update_traces(textposition='auto')
                    fig_bar_overdue_days_2.update_layout(
                        xaxis_title="Customer",
                        yaxis_title="Average Overdue Days",
                        xaxis_tickangle=45
                    )
                    st.plotly_chart(fig_bar_overdue_days_2, use_container_width=True, key="customer_overdue_days_chart")                                     
        else:
            st.info("No overdue data available for the selected DSS.")                
    else:
        st.info("Overdue data not available. Please ensure 'days_overdue' is calculated.")

    # Dashboard Visualizations
    st.markdown("##### Dashboard Visualizations")
    
    # Side-by-Side Pie Charts: Remaining Balance and Overdue Amount by Customer
    if not filtered_df.empty:
        # Prepare data for Remaining Balance Pie Chart
        customer_balance = filtered_df.groupby('customer_name').agg({
            'remaining_balance': 'sum'
        }).reset_index()
        customer_balance = customer_balance[customer_balance['remaining_balance'] > 0]

        # Prepare data for Overdue Amount Pie Chart
        if 'days_overdue' in filtered_df.columns:
            overdue_df = filtered_df[(filtered_df['days_overdue'] > 0) & (filtered_df['remaining_balance'] > 0)].copy()
            customer_overdue = overdue_df.groupby('customer_name').agg({
                'remaining_balance': 'sum'
            }).reset_index()
            customer_overdue = customer_overdue[customer_overdue['remaining_balance'] > 0]
        else:
            customer_overdue = pd.DataFrame()

        # Create two columns for side-by-side pie charts
        col1, col2 = st.columns(2)

        # Remaining Balance Pie Chart (Left)
        with col1:
            if not customer_balance.empty:
                fig_pie_remaining = px.pie(
                    customer_balance,
                    values='remaining_balance',
                    names='customer_name',
                    title='Remaining Balance Distribution by Customer',
                    color_discrete_sequence=px.colors.qualitative.D3
                )
                fig_pie_remaining.update_traces(textposition='inside', textinfo='percent+label')
                fig_pie_remaining.update_layout(showlegend=True, height=500)
                st.plotly_chart(fig_pie_remaining, use_container_width=True, key='fig_pie_remaining_cr')
            else:
                st.info("No remaining balance data available for customers.")

        # Overdue Amount Pie Chart (Right)
        with col2:
            if not customer_overdue.empty:
                fig_pie_overdue = px.pie(
                    customer_overdue,
                    values='remaining_balance',
                    names='customer_name',
                    title='Overdue Amount Distribution by Customer',
                    color_discrete_sequence=px.colors.qualitative.D3
                )
                fig_pie_overdue.update_traces(
                    textposition='inside',
                    textinfo='percent+label',
                    hovertemplate='<b>%{label}</b><br>Overdue Amount: %{value:,.2f}<br>Percentage: %{percent}'
                )
                fig_pie_overdue.update_layout(showlegend=True, height=500)
                st.plotly_chart(fig_pie_overdue, use_container_width=True, key='fig_pie_overdue_cr')
            else:
                st.info("No overdue amount data available for customers.")
    
    ###############################################################################################################################
    # Aging Visualization for DSS
    ###############################################################################################################################            
    # Grouped Vertical Bar Chart: Remaining Balance by Aging Bucket and Customer
    if 'remaining_balance' in filtered_df.columns and 'inv_dr_date' in filtered_df.columns and 'inv_dr_no' in filtered_df.columns and not overdue_df.empty:
        # Current date and time (naive for compatibility)
        current_datetime = pd.to_datetime('2025-05-16 10:08:00').replace(tzinfo=None)
        
        # Convert inv_dr_date to datetime and calculate duration
        overdue_df['inv_dr_date'] = pd.to_datetime(overdue_df['inv_dr_date'])
        overdue_df['duration_days'] = (current_datetime - overdue_df['inv_dr_date']).dt.days
        
        # Calculate approximate months and days for hover
        overdue_df['months'] = overdue_df['duration_days'].fillna(0).astype(int) // 30
        overdue_df['days'] = overdue_df['duration_days'].fillna(0).astype(int) % 30
        
        # Create aging buckets based on existing duration_days
        def get_aging_bucket(days):
            if days <= 30:
                return "0-30 days"
            elif days <= 60:
                return "31-60 days"
            elif days <= 90:
                return "61-90 days"
            elif days <= 120:
                return "91-120 days"
            else:
                return "120+ days"
        
        overdue_df['aging_bucket'] = overdue_df['duration_days'].apply(get_aging_bucket)
        
        # Aggregate remaining_balance per inv_dr_no and customer_name - including aging_bucket
        agg_data = overdue_df[overdue_df['remaining_balance'] > 0].groupby(['aging_bucket', 'inv_dr_no', 'customer_name']).agg({
            'remaining_balance': 'sum',
            'duration_days': 'mean',
            'months': 'first',
            'days': 'first'
        }).reset_index()
        
        # Define order for aging buckets
        bucket_order = ["0-30 days", "31-60 days", "61-90 days", "91-120 days", "120+ days"]
        agg_data['aging_bucket'] = pd.Categorical(agg_data['aging_bucket'], categories=bucket_order, ordered=True)
        agg_data = agg_data.sort_values('aging_bucket')
        
        # Create the grouped vertical bar chart
        fig_aging = go.Figure()
        
        # Define a color palette for customer_name
        colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
                '#aec7e8', '#ffbb78', '#98df8a', '#ff9896', '#c5b0d5', '#c49c94', '#f7b6d2', '#c7c7c7', '#dbdb8d', '#9edae5']
        
        # Add a bar for each customer_name within each aging bucket
        for idx, customer in enumerate(agg_data['customer_name'].unique()):
            customer_data = agg_data[agg_data['customer_name'] == customer]
            fig_aging.add_trace(go.Bar(
                x=customer_data['aging_bucket'],
                y=customer_data['remaining_balance'],
                name=customer,
                text=[f"Customer: {row['customer_name']}<br>Aging: {row['aging_bucket']}<br>Invoice No: {row['inv_dr_no']}<br>Remaining Balance: {row['remaining_balance']:,.2f}<br>Duration: {int(row['months'])} months, {int(row['days'])} days"
                    for _, row in customer_data.iterrows()],
                hovertemplate='%{text}<extra></extra>',
                marker_color=colors[idx % len(colors)]
            ))
        
        fig_aging.update_layout(
            barmode='group',
            title='Remaining Balance by Aging Bucket (Grouped by Customer)',
            xaxis_title="Aging Bucket",
            yaxis_title="Remaining Balance",
            height=500,
            xaxis={'categoryorder': 'array', 'categoryarray': bucket_order},
            legend_title="Customer"
        )
        st.plotly_chart(fig_aging, use_container_width=True, key='fig_aging_cr')
    else:
        st.info("Visualization data not available. Please ensure overdue data and required columns are present.")
        
    
    
@st.fragment
def Overdue_fragment():
    st.markdown("#### Overdue Summary")    
    result_df = st.session_state.result_df
    date_from_str = st.session_state.date_from_str
    date_to_str = st.session_state.date_to_str
    
    # Reuse DSM filter from tab1
    st.markdown("##### Filter by DSM")
    dsm_summary = result_df.groupby(['dsm', 'dept', 'dept_code']).agg({'remaining_balance': 'sum'}).reset_index()
    dsm_summary = dsm_summary[dsm_summary['remaining_balance'] > 0].groupby(['dsm', 'dept_code']).agg({
        'remaining_balance': 'sum',
        'dept': 'first'
    }).reset_index()
    dsm_summary = dsm_summary.sort_values(by=['dsm','dept_code'])
    
    dsm_options = ['All'] + [f"{row['dsm']} - (Remaining Balance: PHP {row['remaining_balance']:,.2f}, Dept: {row['dept_code']})" 
                        for _, row in dsm_summary.iterrows()]
    
    selected_dsm = st.selectbox("Select DSM", dsm_options, key="dsm_dashboard_selectbox")
    
    selected_dsm_value = selected_dsm if selected_dsm == 'All' else selected_dsm.split(' - (')[0]
    selected_dept_value = None if selected_dsm == 'All' else selected_dsm.split('Dept: ')[1].rstrip(')')
    
    if selected_dsm_value == 'All':
        filtered_df = result_df[result_df['remaining_balance'].notna() & (result_df['remaining_balance'] != 0)]
    else:
        filtered_df = result_df[(result_df['dsm'] == selected_dsm_value) & 
                            (result_df['dept_code'].astype(str) == selected_dept_value) &
                            result_df['remaining_balance'].notna() & 
                            (result_df['remaining_balance'] != 0)]
    
    # Overdue Report
    st.markdown("##### Overdue Report by PMR and Customer")
    if 'days_overdue' in filtered_df.columns:
        overdue_df = filtered_df[(filtered_df['days_overdue'] > 0) & (filtered_df['remaining_balance'] > 0)].copy()
        if not overdue_df.empty:
            overdue_report = overdue_df.groupby(['pmr', 'customer_name']).agg({
                'remaining_balance': 'sum',
                'days_overdue': 'mean'
            }).reset_index()
            overdue_report = overdue_report.rename(columns={
                'remaining_balance': 'Overdue Amount (PHP)',
                'days_overdue': 'Average Days Overdue'
            })
            overdue_report = overdue_report.sort_values(by='Overdue Amount (PHP)', ascending=False)
            
            display_overdue_df = overdue_report.copy()

            col_cfg_overdue = _numeric_column_config(display_overdue_df)
            st.dataframe(display_overdue_df, use_container_width=True, hide_index=True, column_config=col_cfg_overdue)

            overdue_csv = overdue_report.to_csv(index=False)
            st.download_button(
                label="Download Overdue Report",
                data=overdue_csv,
                file_name=f"Overdue_Report_DSM_{selected_dsm_value}_{date_from_str}_to_{date_to_str}.csv",
                mime="text/csv"
            )
            
            # Create two columns for side-by-side pie charts
            cols1, cols2 = st.columns(2)
            with cols1:
                # Bar Chart: Overdue Amount by PMR
                st.markdown("##### Overdue Amount by PMR")
                fig_bar = px.bar(
                    overdue_report,
                    x='pmr',
                    y='Overdue Amount (PHP)',
                    title='Overdue Amount by PMR',
                    color='Overdue Amount (PHP)',
                    color_continuous_scale='Reds',
                    text_auto='.2s'
                )
                fig_bar.update_layout(
                    xaxis_title="PMR",
                    yaxis_title="Overdue Amount (PHP)",
                    xaxis_tickangle=45
                )
                st.plotly_chart(fig_bar, use_container_width=True)
            
            with cols2:   
                # Bar Chart: Customer with Overdue Days
                if 'days_overdue' in filtered_df.columns and not overdue_df.empty:
                    company_overdue = overdue_df.groupby('customer_name').agg({
                        'days_overdue': 'mean'
                    }).reset_index()
                    company_overdue = company_overdue.sort_values(by='days_overdue', ascending=False)
                    
                    st.markdown("##### Customer with Overdue Days")
                    fig_bar_overdue_days = px.bar(
                        company_overdue,
                        x='customer_name',
                        y='days_overdue',
                        title='Customer by Average Overdue Days',
                        color='days_overdue',
                        color_continuous_scale='Reds',
                        text='days_overdue',
                        height=500
                    )
                    fig_bar_overdue_days.update_traces(textposition='auto')
                    fig_bar_overdue_days.update_layout(
                        xaxis_title="Customer",
                        yaxis_title="Average Overdue Days",
                        xaxis_tickangle=45
                    )
                    st.plotly_chart(fig_bar_overdue_days, use_container_width=True)                                     
        else:
            st.info("No overdue data available for the selected DSM.")                
    else:
        st.info("Overdue data not available. Please ensure 'days_overdue' is calculated.")
    
    # Dashboard Visualizations
    st.markdown("##### Dashboard Visualizations")
    
    # Side-by-Side Pie Charts: Remaining Balance and Overdue Amount by Customer
    if not filtered_df.empty:
        # Prepare data for Remaining Balance Pie Chart
        customer_balance = filtered_df.groupby('customer_name').agg({
            'remaining_balance': 'sum'
        }).reset_index()
        customer_balance = customer_balance[customer_balance['remaining_balance'] > 0]

        # Prepare data for Overdue Amount Pie Chart
        if 'days_overdue' in filtered_df.columns:
            overdue_df = filtered_df[(filtered_df['days_overdue'] > 0) & (filtered_df['remaining_balance'] > 0)].copy()
            customer_overdue = overdue_df.groupby('customer_name').agg({
                'remaining_balance': 'sum'
            }).reset_index()
            customer_overdue = customer_overdue[customer_overdue['remaining_balance'] > 0]
        else:
            customer_overdue = pd.DataFrame()

        # Create two columns for side-by-side pie charts
        col1, col2 = st.columns(2)

        # Remaining Balance Pie Chart (Left)
        with col1:
            if not customer_balance.empty:
                fig_pie_remaining = px.pie(
                    customer_balance,
                    values='remaining_balance',
                    names='customer_name',
                    title='Remaining Balance Distribution by Customer',
                    color_discrete_sequence=px.colors.qualitative.D3
                )
                fig_pie_remaining.update_traces(textposition='inside', textinfo='percent+label')
                fig_pie_remaining.update_layout(showlegend=True, height=500)
                st.plotly_chart(fig_pie_remaining, use_container_width=True)
            else:
                st.info("No remaining balance data available for customers.")

        # Overdue Amount Pie Chart (Right)
        with col2:
            if not customer_overdue.empty:
                fig_pie_overdue = px.pie(
                    customer_overdue,
                    values='remaining_balance',
                    names='customer_name',
                    title='Overdue Amount Distribution by Customer',
                    color_discrete_sequence=px.colors.qualitative.D3
                )
                fig_pie_overdue.update_traces(textposition='inside', textinfo='percent+label')
                fig_pie_overdue.update_layout(showlegend=True, height=500)
                st.plotly_chart(fig_pie_overdue, use_container_width=True)
            else:
                st.info("No overdue amount data available for customers.")
    ###############################################################################################################################
    # Aging Visualization for DSM
    ###############################################################################################################################            
    # Grouped Vertical Bar Chart: Remaining Balance by Aging Bucket and Customer
    if 'remaining_balance' in filtered_df.columns and 'inv_dr_date' in filtered_df.columns and 'inv_dr_no' in filtered_df.columns and not overdue_df.empty:
        # Current date and time (naive for compatibility)
        current_datetime = pd.to_datetime('2025-05-16 10:08:00').replace(tzinfo=None)
        
        # Convert inv_dr_date to datetime and calculate duration
        overdue_df['inv_dr_date'] = pd.to_datetime(overdue_df['inv_dr_date'])
        overdue_df['duration_days'] = (current_datetime - overdue_df['inv_dr_date']).dt.days
        
        # Calculate approximate months and days for hover
        overdue_df['months'] = overdue_df['duration_days'].fillna(0).astype(int) // 30
        overdue_df['days'] = overdue_df['duration_days'].fillna(0).astype(int) % 30
        
        # Create aging buckets based on existing duration_days
        def get_aging_bucket(days):
            if days <= 30:
                return "0-30 days"
            elif days <= 60:
                return "31-60 days"
            elif days <= 90:
                return "61-90 days"
            elif days <= 120:
                return "91-120 days"
            else:
                return "120+ days"
        
        overdue_df['aging_bucket'] = overdue_df['duration_days'].apply(get_aging_bucket)
        
        # Aggregate remaining_balance per inv_dr_no and customer_name - including aging_bucket
        agg_data = overdue_df[overdue_df['remaining_balance'] > 0].groupby(['aging_bucket', 'inv_dr_no', 'customer_name']).agg({
            'remaining_balance': 'sum',
            'duration_days': 'mean',
            'months': 'first',
            'days': 'first'
        }).reset_index()
        
        # Define order for aging buckets
        bucket_order = ["0-30 days", "31-60 days", "61-90 days", "91-120 days", "120+ days"]
        agg_data['aging_bucket'] = pd.Categorical(agg_data['aging_bucket'], categories=bucket_order, ordered=True)
        agg_data = agg_data.sort_values('aging_bucket')
        
        # Create the grouped vertical bar chart
        fig_aging = go.Figure()
        
        # Define a color palette for customer_name
        colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
                '#aec7e8', '#ffbb78', '#98df8a', '#ff9896', '#c5b0d5', '#c49c94', '#f7b6d2', '#c7c7c7', '#dbdb8d', '#9edae5']
        
        # Add a bar for each customer_name within each aging bucket
        for idx, customer in enumerate(agg_data['customer_name'].unique()):
            customer_data = agg_data[agg_data['customer_name'] == customer]
            fig_aging.add_trace(go.Bar(
                x=customer_data['aging_bucket'],
                y=customer_data['remaining_balance'],
                name=customer,
                text=[f"Customer: {row['customer_name']}<br>Aging: {row['aging_bucket']}<br>Invoice No: {row['inv_dr_no']}<br>Remaining Balance: {row['remaining_balance']:,.2f}<br>Duration: {int(row['months'])} months, {int(row['days'])} days"
                    for _, row in customer_data.iterrows()],
                hovertemplate='%{text}<extra></extra>',
                marker_color=colors[idx % len(colors)]
            ))
        
        fig_aging.update_layout(
            barmode='group',
            title='Remaining Balance by Aging Bucket (Grouped by Customer)',
            xaxis_title="Aging Bucket",
            yaxis_title="Remaining Balance",
            height=500,
            xaxis={'categoryorder': 'array', 'categoryarray': bucket_order},
            legend_title="Customer"
        )
        st.plotly_chart(fig_aging, use_container_width=True)
    else:
        st.info("Visualization data not available. Please ensure overdue data and required columns are present.")
        
        

@st.fragment
def target_base_fragment():
    st.subheader("Target Base Collection Performance Summary")
    # st.markdown("#### Target Base Collection Performance")
    
    # Ensure result_df7 is available in session state SCR_NAME
    if 'result_df7' not in st.session_state:
        st.error("Error: result_df7 not found in session state. Please ensure data is loaded.")
    else:
        st_result_df = st.session_state.result_df7.copy()
        st_result_df = st_result_df[(st_result_df['DocumentType'] == 'INVOICE') &
                                    (~st_result_df['DocumentNo'].fillna('').str.startswith(('PSCM', 'JV')))].copy()
        
        # merged_df7_ov = merged_df7_ov[                                                
        # (~merged_df7_ov['DocumentNo'].fillna('').str.startswith(('PSCM', 'JV'))) & 
        # (~merged_df7_ov['JournalBatchName'].str.contains('EWT|WHT', na=False)) & 
        # (~merged_df7_ov['BalAccountNo'].str.contains('EWT|WHT', na=False)) & 
        # (merged_df7_ov['DocumentType'] == 'INVOICE')
        
        st_result_df = st_result_df.reset_index(drop=True)
                       
        # Convert CollectedAmount to numeric, handling missing or invalid values
        if 'CollectedAmount' in st_result_df.columns:
            st_result_df['CollectedAmount'] = pd.to_numeric(st_result_df['CollectedAmount'], errors='coerce').fillna(0)
        else:
            # st_result_df['CollectedAmount'] = 0
            st.warning("Warning: 'CollectedAmount' column not found. Initialized to 0.")
        st_result_df_csv = st_result_df[['SCR_NAME', 'CollectedAmount']].copy()  # noqa: F841
        # st_result_df_csv.to_csv('st_result_df_debug_total_target.csv', index=False)  # Debugging line to inspect the DataFrame structure
        # Cache the initial data aggregation in session state
        if 'initial_performance_df' not in st.session_state:
            # Get dynamic columns starting after 'BalAccountNo'
            try:
                dynamic_columns = [col for col in st_result_df.columns[st_result_df.columns.get_loc('BalAccountNo') + 1:]
                                if col.startswith('%_')]
            except KeyError:
                st.error("Error: 'BalAccountNo' column not found in st_result_df.")
                dynamic_columns = []  # noqa: F841

            # Calculate collection performance
            performance_data = []
            
            if 'SCR_NAME' in st_result_df.columns:
                for cr in st_result_df['SCR_NAME'].unique():
                    cr_df = st_result_df[st_result_df['SCR_NAME'] == cr]
                    
                    # Initialize totals
                    total_collected = 0
                    collection_count = 0
                    
                    # Sum CollectedAmount for the SCR
                    if 'CollectedAmount' in cr_df.columns:
                        total_collected = cr_df['CollectedAmount'].sum()
                        collection_count = (cr_df['CollectedAmount'] < 0).sum()  # Count negative collections  # noqa: F841
                    # print(cr)
                    # print(cr, total_collected, collection_count)
                    # print(total_collected)
                    # Calculate total initial amount
                    total_initial = cr_df['Gross Amount'].sum() if 'Gross Amount' in cr_df.columns else 0

                    # Sum TARGET from filtered_CURRENT and filtered_COD with numeric conversion and cleaning
                    target_current = 0
                    target_cod = 0
                    target_overdue = 0
                    total_returns = 0
                    total_adjustments = 0
                    print()
                    if 'filtered_CURRENT' in st.session_state and not st.session_state.filtered_CURRENT.empty:
                        # print("CURRENT DataFrame found in session state.")
                        current_df = st.session_state.filtered_CURRENT.copy()
                        if 'SCR_NAME' in current_df.columns and 'Total Collected Amount (PHP)' in current_df.columns:
                            # Clean the column by removing non-numeric characters and converting to numeric
                            current_df['Total Collected Amount (PHP)'] = current_df['Total Collected Amount (PHP)'].astype(str).str.replace(r'[^\d.-]', '', regex=True)
                            current_df['Total Collected Amount (PHP)'] = pd.to_numeric(current_df['Total Collected Amount (PHP)'], errors='coerce').fillna(0)
                            current_subset = current_df[current_df['SCR_NAME'] == cr]
                            target_current = current_subset['Total Collected Amount (PHP)'].sum()
                    if 'filtered_COD' in st.session_state and not st.session_state.filtered_COD.empty:
                        # print("COD DataFrame found in session state.")
                        cod_df = st.session_state.filtered_COD.copy()
                        if 'SCR_NAME' in cod_df.columns and 'Total Collected Amount (PHP)' in cod_df.columns:
                            # Clean the column by removing non-numeric characters and converting to numeric
                            cod_df['Total Collected Amount (PHP)'] = cod_df['Total Collected Amount (PHP)'].astype(str).str.replace(r'[^\d.-]', '', regex=True)
                            cod_df['Total Collected Amount (PHP)'] = pd.to_numeric(cod_df['Total Collected Amount (PHP)'], errors='coerce').fillna(0)
                            cod_subset = cod_df[cod_df['SCR_NAME'] == cr]
                            target_cod = cod_subset['Total Collected Amount (PHP)'].sum()
                    if 'filtered_OVERDUE' in st.session_state and not st.session_state.filtered_OVERDUE.empty:
                        # print("OVERDUE DataFrame found in session state.")
                        overdue_df = st.session_state.filtered_OVERDUE.copy()
                        if 'SR2' in overdue_df.columns and 'Total Overdue Amount (PHP)' in overdue_df.columns:
                            # st.warning("Processing overdue_df for target calculation.")
                            # Clean the column by removing non-numeric characters and converting to numeric
                            overdue_df['Total Overdue Amount (PHP)'] = overdue_df['Total Overdue Amount (PHP)'].astype(str).str.replace(r'[^\d.-]', '', regex=True)
                            overdue_df['Total Overdue Amount (PHP)'] = pd.to_numeric(overdue_df['Total Overdue Amount (PHP)'], errors='coerce').fillna(0)
                            overdue_subset = overdue_df[overdue_df['SR2'] == cr]
                            target_overdue = overdue_subset['Total Overdue Amount (PHP)'].sum()  
                            
                    # Add filtered_RETURNS
                    if 'filtered_RETURNS_GROUPBY' in st.session_state and not st.session_state.filtered_RETURNS_GROUPBY.empty:
                        # print("RETURNS DataFrame found in session state.")
                        returns_df = st.session_state.filtered_RETURNS_GROUPBY.copy()
                        if 'SCR_NAME' in returns_df.columns and 'Collected_Return' in returns_df.columns:
                            # debugging line to check the content of returns_df
                            # st.warning("Processing returns_df for target calculation.") 
                                                                        
                            # Clean the column by removing non-numeric characters and converting to numeric
                            returns_df['Collected_Return'] = returns_df['Collected_Return'].astype(str).str.replace(r'[^\d.-]', '', regex=True)
                            returns_df['Collected_Return'] = pd.to_numeric(returns_df['Collected_Return'], errors='coerce').fillna(0)
                            returns_subset = returns_df[returns_df['SCR_NAME'] == cr]
                            total_returns = returns_subset['Collected_Return'].sum()                                            
                            
                    if 'filtered_ADJUSTMENTS_GROUPBY' in st.session_state and not st.session_state.filtered_ADJUSTMENTS_GROUPBY.empty:
                        # print("ADJUSTMENTS DataFrame found in session state.")
                        adjustments_df = st.session_state.filtered_ADJUSTMENTS_GROUPBY.copy()
                        if 'SCR_NAME' in adjustments_df.columns and 'Balance Due' in adjustments_df.columns:
                            # st.warning("Processing adjustments_df for target calculation.") 
                            # Clean the column by removing non-numeric characters and converting to numeric
                            adjustments_df['Balance Due'] = adjustments_df['Balance Due'].astype(str).str.replace(r'[^\d.-]', '', regex=True)
                            adjustments_df['Balance Due'] = pd.to_numeric(adjustments_df['Balance Due'], errors='coerce').fillna(0)
                            adjustments_subset = adjustments_df[adjustments_df['SCR_NAME'] == cr]
                            total_adjustments = adjustments_subset['Balance Due'].sum()
                                                                            
                    # Calculate total target                                                                                              
                    target = abs(target_current) + abs(target_cod) + abs(target_overdue)
                    
                    # print("Processing Target for SCR:", cr)
                    # print("Target Current:", target_current)
                    # print("Target COD:", target_cod)
                    # print("Target Overdue:", target_overdue)
                    # print("Total Returns:", total_returns)  
                    # print("Total Adjustments:", total_adjustments)
        
                    performance_data.append({
                        'SCR': cr,            
                        'Total Collected Amount (PHP)': float(abs(total_collected)),                                   
                        'OVERDUE' : float(abs(target_overdue)),
                        'CURRENT' : float(abs(target_current)), 
                        'COD' : float(abs(target_cod)),    
                        'TOTAL TARGET': float(abs(target)),                     
                        'EWT/WHT': 0.0,
                        'RETURNS': float(abs(total_returns)),
                        'Other Adjustments': float(abs(total_adjustments)),                                                                     
                        'NET TARGET': 0.0,
                        'Collection Perf Rate (%)': 0.0 if total_initial == 0 else (abs(total_collected) / total_initial) * 100,
                    })
               
               
                # Create performance DataFrame
                performance_df = pd.DataFrame(performance_data)
                performance_df = performance_df.sort_values(by='SCR', ascending=True)
                
                st.session_state.initial_performance_df = performance_df.copy()
                                                                            
            else:
                st.error("Error: 'SCR_NAME' column not found in st_result_df.")
                                                                        
            # Initialize/update the editable state
            if 'performance_df_state' not in st.session_state:
                st.session_state.performance_df_state = st.session_state.initial_performance_df.copy()
            
            # Display the data editor (column_config formats numeric for display)
            col_cfg_perf = _numeric_column_config(st.session_state.performance_df_state)
            st.data_editor(
                st.session_state.performance_df_state,
                num_rows="dynamic",
                key="performance_editor",
                on_change=df_on_change,
                disabled=['Total Collected Amount (PHP)', 'NET TARGET', 'Collection Perf Rate (%)'],
                column_config=col_cfg_perf
            )
        
        # Initial Computation on load base on Compute Button
        if 'computed' not in st.session_state:
            with st.spinner("Computing Initial Values..."):
                df_on_change()  # First: Updates NET TARGET
                # Force a micro-refresh just for this function's internal state
                st.session_state.computed = st.session_state.performance_df_state.copy()  # Snapshot
                df_on_change()  # Second: Now uses updated values for perf rate
                del st.session_state.computed  # Cleanup

        if st.button("Compute"):
            with st.spinner("Updating..."):
                df_on_change()  # First: Updates NET TARGET
                # Force a micro-refresh just for this function's internal state
                st.session_state.temp_df = st.session_state.performance_df_state.copy()  # Snapshot
                df_on_change()  # Second: Now uses updated values for perf rate
                del st.session_state.temp_df  # Cleanup
            
@st.fragment
def selectbox_fragments():
    # Initialize 'ADD Days' column if it doesn't exist
    if 'ADD Days' not in st.session_state.display_df6s.columns:
        st.session_state.display_df6s['ADD Days'] = 0

    # Initialize notification state
    if 'notification' not in st.session_state:
        st.session_state.notification = None
        st.session_state.notification_time = None

    # Put 2 Columns This Option Selected DSS Names to include +30 in [Add Days] column and for AR for Collection Performance
    # make the st_col1 thinner than st_col2 to make this look better making it 1:8 ratio
    
    st_col1, st_col2 = st.columns([1, 7])  # Adjust the ratio as needed
    with st_col1:
        st.title(" ")
        # st.subheader("Selected DSS Names to include +30 in [Add Days] column")
        # Use DSS2_Name as key column; fallback to DSS_NAME if DSS2_Name not present
        key_col = 'DSS2_Name' if 'DSS2_Name' in st.session_state.display_df6s.columns else 'DSS_NAME'
        if 'selected_dss' not in st.session_state:
            st.session_state.selected_dss = None 
        if 'selected_sr2' not in st.session_state:
            st.session_state.selected_sr2 = ''
        if 'display_df' not in st.session_state or key_col not in st.session_state.display_df.columns:
            st.session_state.display_df = pd.DataFrame(columns=[key_col, 'SR2'])           
        if 'SR2' not in st.session_state.display_df.columns:
            st.session_state.display_df['SR2'] = ''
        # Get the DataFrame from session state
        st.session_state.df_add_30days = st.session_state.display_df6s[[key_col]].copy()
        # Get unique values only 
        st.session_state.df_add_30days = st.session_state.df_add_30days.drop_duplicates().reset_index(drop=True)
        # Remove blank or null values and normalize
        st.session_state.df_add_30days = st.session_state.df_add_30days[st.session_state.df_add_30days[key_col].notna() & (st.session_state.df_add_30days[key_col] != '')]
        st.session_state.df_add_30days[key_col] = st.session_state.df_add_30days[key_col].astype(str).str.strip()
        # Sort by key column
        dss_summary = st.session_state.df_add_30days.sort_values(by=[key_col])                                                                                            
        
        # Create custom labels for the selectbox
        dss_options = [row[key_col] for _, row in dss_summary.iterrows()]

        # Selectbox for choosing DSS
        selected_dss = st.selectbox("Select DSS2 (+30 days)", dss_options, key="dss_selectbox_add_30days")
        st.session_state.selected_dss = selected_dss

        # Build SR2 options: empty (default = all SR2) + unique SR2 from display_df6s
        sr2_unique = []
        if 'SR2' in st.session_state.display_df6s.columns:
            sr2_vals = st.session_state.display_df6s['SR2'].dropna().astype(str).str.strip().unique()
            sr2_unique = sorted([v for v in sr2_vals if v])
        sr2_options = [''] + sr2_unique

        # Selectbox for choosing SR2 (empty = all SR2 for selected DSS2)
        selected_sr2 = st.selectbox("Select SR2 (+30 days)", sr2_options, key="sr2_selectbox_add_30days", index=0) if sr2_options else ''
        st.session_state.selected_sr2 = selected_sr2 if sr2_options else ''

        # The selected_dss is directly the DSS2_Name (or DSS_NAME fallback)
        selected_dss_name = selected_dss.strip() if selected_dss else selected_dss
        # Empty means match all SR2 for that DSS2_Name
        selected_sr2_val = None if (not selected_sr2 or (isinstance(selected_sr2, str) and not selected_sr2.strip())) else selected_sr2.strip()

        # Create a placeholder for notifications
        notification_placeholder = st.empty()

        # Create two columns for Add and Delete buttons
        col1, col2 = st.columns(2)

        with col1:
            if st.button("Add"):
                # Build selected_row with key_col and SR2
                _sr2_store = (selected_sr2 or '').strip() if isinstance(selected_sr2, str) else ''
                selected_row = pd.DataFrame({key_col: [selected_dss_name], 'SR2': [_sr2_store]})
                # Check if (key_col, SR2) combo already in display_df
                _sr2_df = st.session_state.display_df['SR2'].fillna('').astype(str).str.strip()
                dup_mask = (st.session_state.display_df[key_col] == selected_dss_name) & (_sr2_df == _sr2_store)
                if not dup_mask.any():
                    st.session_state.display_df = pd.concat(
                        [st.session_state.display_df, selected_row],
                        ignore_index=True
                    )
                # Update ADD Days to 30 for matching rows in display_df6s, excluding grand total row
                data_rows = st.session_state.display_df6s.iloc[:-1].copy()  # Exclude grand total
                if key_col in data_rows.columns:
                    data_rows[key_col] = data_rows[key_col].astype(str).str.strip()
                dss_match = data_rows[key_col] == selected_dss_name if key_col in data_rows.columns else pd.Series([False] * len(data_rows), index=data_rows.index)
                # If SR2 selected (not empty), add SR2 condition
                if selected_sr2_val is not None and 'SR2' in data_rows.columns:
                    data_rows['SR2_norm'] = data_rows['SR2'].fillna('').astype(str).str.strip()
                    sr2_match = data_rows['SR2_norm'] == selected_sr2_val
                    dss_match = dss_match & sr2_match
                bal_col_dss = next((c for c in ['Balance Due', 'Remaining Balance', 'BalanceDue'] if c in data_rows.columns), None)
                balance_positive_dss = pd.to_numeric(data_rows[bal_col_dss], errors='coerce').fillna(0) >= 0 if bal_col_dss else pd.Series([True] * len(data_rows), index=data_rows.index)
                apply_mask = dss_match & balance_positive_dss
                num_rows = len(data_rows[apply_mask])
                data_rows.loc[apply_mask, 'ADD Days'] = 30
                st.session_state.notification = f"Updated {num_rows} rows for {key_col}: {selected_dss_name}" + (f" / SR2: {selected_sr2_val}" if selected_sr2_val else " (All SR2)")
                st.session_state.notification_time = time.time()
                updated_df = update_calculations(data_rows)
                grand_total = st.session_state.display_df6s.iloc[-1:].copy()
                st.session_state.display_df6s = pd.concat([updated_df, grand_total], ignore_index=True)

        with col2:
            if st.button("Del"):
                # Remove the (key_col, SR2) combo from display_df
                _sr2_del = (selected_sr2 or '').strip() if isinstance(selected_sr2, str) else ''
                sr2_display = st.session_state.display_df['SR2'].fillna('').astype(str).str.strip()
                remove_mask = (st.session_state.display_df[key_col] == selected_dss_name) & (sr2_display == _sr2_del)
                st.session_state.display_df = st.session_state.display_df[~remove_mask].reset_index(drop=True)
                # Reset ADD Days to 0 for matching rows in display_df6s
                data_rows = st.session_state.display_df6s.iloc[:-1].copy()  # Exclude grand total
                if key_col in data_rows.columns:
                    data_rows[key_col] = data_rows[key_col].astype(str).str.strip()
                dss_match = data_rows[key_col] == selected_dss_name if key_col in data_rows.columns else pd.Series([False] * len(data_rows), index=data_rows.index)
                if selected_sr2_val is not None and 'SR2' in data_rows.columns:
                    data_rows['SR2_norm'] = data_rows['SR2'].fillna('').astype(str).str.strip()
                    sr2_match = data_rows['SR2_norm'] == selected_sr2_val
                    dss_match = dss_match & sr2_match
                num_rows = dss_match.sum()
                data_rows.loc[dss_match, 'ADD Days'] = 0
                st.session_state.notification = f"Reset {num_rows} rows for {key_col}: {selected_dss_name}" + (f" / SR2: {selected_sr2_val}" if selected_sr2_val else " (All SR2)")
                st.session_state.notification_time = time.time()
                updated_df = update_calculations(data_rows)
                grand_total = st.session_state.display_df6s.iloc[-1:].copy()
                st.session_state.display_df6s = pd.concat([updated_df, grand_total], ignore_index=True)

        # Display and clear notification
        if st.session_state.notification:
            with notification_placeholder:
                if "Updated" in st.session_state.notification:
                    st.success(st.session_state.notification)
                else:
                    st.info(st.session_state.notification)
            # Clear notification after 3 seconds
            if time.time() - st.session_state.notification_time > 1:
                st.session_state.notification = None
                st.session_state.notification_time = None
                notification_placeholder.empty()

        # Display the selected DSS2 + SR2 combinations

        if not st.session_state.display_df.empty:  
            st.markdown("##### Currently Selected DSS2 + SR2:")      
            for _, row in st.session_state.display_df.iterrows():
                dss_val = row[key_col]
                sr2_val = row.get('SR2', '')
                if pd.isna(sr2_val) or str(sr2_val).strip() == '':
                    st.markdown(f"- {dss_val} (All SR2)")
                else:
                    st.markdown(f"- {dss_val} / {sr2_val}")
        else:
            st.markdown("##### \n*No DSS selected yet.*")
        
        # Add horizontal line for separation
        st.markdown("---")
        
    with st_col2:        
        AR_with_add_days_fragment()
        
# Cached function for df7
@st.cache_data
def load_df7(_connection, date_from_str, date_to_str, sproc7):
    query7 = text(f"EXEC [dbo].[{sproc7}] '{date_from_str}', '{date_to_str}'")
    df7 = pd.read_sql(query7, _connection)
    # Assuming your DataFrame is called 'df'
    
    
    #########################################
    # FOR SAFETY REMOVE DUPLICATES BASED ON EntryNo and other similar columns
    # This can be ignore or remove if the Query in stored procedure is already fixed.
    columns = ["EntryNo", "PostingDate", "DueDate", "DocumentNo", "DocumentType", "Amount", 
            "CustomerNo", "ExternalDocumentNo", "DetailDoc", "ClosedByEntryNo", 
            "CollectedAmount","Collected_EWT","Collected_Return", "AppliedCustLedgrNo", "SCR_NAME"]
    # "JournalBatchName", "BalAccountNo" was removed for accuracy of drop_duplicates
    # Convert blank or null values to a specific string (e.g., 'ZZZZZ') for sorting purposes
    df7['Re_Tag_CR_Code'] = df7['Re_Tag_CR_Code'].fillna('ZZZZZ').replace('', 'ZZZZZ')

    # Sort by Re_Tag_CR_Code and Re_Tag_CR_Name, ensuring blanks (now 'ZZZZZ') are last
    df7 = df7.sort_values(by=["Re_Tag_CR_Code", "Re_Tag_CR_Name"], na_position='last')

    # Save initial load for debugging
    # df7.to_csv('debug_df7_initial_load.csv', index=False)

    # Drop duplicates, keeping the first occurrence
    df7 = df7.drop_duplicates(subset=columns, keep='first').reset_index(drop=True)
    # Save initial load for debugging
    # df7.to_csv('debug_df7_after_drop.csv', index=False) 
    # Optionally, revert 'ZZZZZ' back to NaN or empty if needed
    df7['Re_Tag_CR_Code'] = df7['Re_Tag_CR_Code'].replace('ZZZZZ', np.nan)    
    #
    #########################################
    
    df7['SCR'] = df7.apply(lambda row: row['Re_Tag_CR_Code'] if pd.notnull(row['Re_Tag_CR_Code']) and row['Re_Tag_CR_Code'].strip() != '' else row['SCR'], axis=1)
    df7['SCR_NAME'] = df7.apply(lambda row: row['Re_Tag_CR_Name'] if pd.notnull(row['Re_Tag_CR_Name']) and row['Re_Tag_CR_Name'].strip() != '' else row['SCR_NAME'], axis=1)
    df7.rename(columns={'Amount': 'Gross Amount', 'PaidUnpaid': 'Remaining Balance'}, inplace=True)
    df7 = df7.drop(columns=['Blank_Date','ReasonCode','DocumentTypeNo','ClosedByEntryNo'], errors='ignore')
    return df7

@st.cache_data
def load_df7a(_connection, date_from_str, date_to_str, sproc10):
    query10 = text(f"EXEC [dbo].[{sproc10}] '{date_from_str}', '{date_to_str}'")
    df10 = pd.read_sql(query10, _connection)
    return df10

# Cached function for df9
@st.cache_data
def load_df9(_connection, sproc9):
    query9 = text(f"EXEC [dbo].[{sproc9}]")
    df9 = pd.read_sql(query9, _connection)
    df9_columns = ['Entry No_',	'Customer No_',	'Posting Date',	'Document Type',	
                    'Document No_',	'Description',	'Sell-to Customer No_',	'Customer Posting Group',	
                    'Global Dimension 1 Code',	'Global Dimension 2 Code',	'Due Date',	'Closed by Entry No_','Bal_ Account No_', 
                    'Closed at Date', 'Closed by Amount','Document Date','External Document No_','Dimension Set ID', 'Journal Batch Name']
    df9 = df9[df9_columns]
    return df9

def same_month1(date_str):
    try:
        # Split the date string into all dates
        dates = date_str.split(' ; ')
        
        # If there are 0 or 1 dates, there can't be a pair with the same year and month
        if len(dates) <= 1:
            return False
        
        # Convert all to datetime
        parsed_dates = [pd.to_datetime(date.strip(), format='%m/%d/%Y') for date in dates]
        
        # Get the year and month of the first date
        reference_date = parsed_dates[0]
        reference_year_month = (reference_date.year, reference_date.month)
        
        # Compare the first date's year and month with others
        for date in parsed_dates[1:]:  # Skip the first date and compare with the rest
            if (date.year, date.month) == reference_year_month:
                return True
        
        return False
    
    except Exception:
        # Catch any parsing errors or other exceptions and return False
        return False

def same_month(date_str):
    try:
        # Split the date string into all dates
        dates = date_str.split(' ; ')

        # If there are 0 or 1 dates, there can't be a pair with the same year and month.
        if len(dates) <= 1:
            return False

        # Convert all to datetime
        parsed_dates = [pd.to_datetime(date.strip(), format='%m/%d/%Y') for date in dates]
        year_months = [(date.year, date.month) for date in parsed_dates]
        counts = Counter(year_months)

        # Check if any year-month combination has a count greater than 1
        for count in counts.values():
            if count > 1:
                return True
        return False

    except Exception:
        # Catch any parsing errors or other exceptions and return False
        return False
    
def check_current(date_str):
    try:
        # Split the date string into two dates
        date1_str, date2_str = date_str.split(' ; ')
        # Convert to datetime
        date1 = pd.to_datetime(date1_str, format='%m/%d/%Y')
        date2 = pd.to_datetime(date2_str, format='%m/%d/%Y')
        # Check if dates have a 1-month difference
        month_diff = abs((date2.year - date1.year) * 12 + date2.month - date1.month) == 1
        # print(abs(date2.month - date1.month))
        return month_diff
    except:  # noqa: E722
        return False  # Return False if parsing fails

def apply_current_bucket_adjustment(df, reference_date):
    """
    Adjust Due Date for specific customers to keep them in Current bucket (AgingDays < 1).
    Reads customer codes from Current_Bucket_Customers.csv and adjusts Due Date accordingly.
    This ensures these customers always appear in the Current aging bucket.
    
    Args:
        df: DataFrame with 'Customer No_' and 'Due Date' columns
        reference_date: Reference date for calculating aging (usually AsOfDate)
    
    Returns:
        DataFrame with adjusted Due Dates for matching customers
    """
    if df.empty or reference_date is None:
        return df
    
    try:
        # Read the CSV file with customer codes that should stay in Current bucket
        current_bucket_df = pd.read_csv('Current_Bucket_Customers.csv')
        
        # Get list of customer codes
        if 'CODE' in current_bucket_df.columns:
            current_bucket_codes = current_bucket_df['CODE'].str.strip().tolist()
        else:
            # Fallback: use first column if CODE column doesn't exist
            current_bucket_codes = current_bucket_df.iloc[:, 0].astype(str).str.strip().tolist()
        
        if not current_bucket_codes:
            return df  # No customers to adjust
        
        # Ensure Customer No_ column exists
        if 'Customer No_' not in df.columns:
            return df
        
        # Find matching customers
        df['Customer No_'] = df['Customer No_'].astype(str).str.strip()
        matching_mask = df['Customer No_'].isin(current_bucket_codes)
        
        if not matching_mask.any():
            return df  # No matching customers found
        
        # Ensure Due Date is datetime
        df['Due Date'] = pd.to_datetime(df['Due Date'], errors='coerce')
        
        # Adjust Due Date: Set it to reference_date or later to ensure AgingDays < 1
        # We set it to reference_date + 1 day to ensure AgingDays is 0 or negative (Current bucket)
        for idx in df[matching_mask].index:
            if pd.notna(df.loc[idx, 'Due Date']) and pd.notna(reference_date):
                current_due_date = df.loc[idx, 'Due Date']
                current_aging = (reference_date - current_due_date).days
                
                # Only adjust if AgingDays >= 1 (not already in Current bucket)
                if current_aging >= 1:
                    # Set Due Date to reference_date + 1 day to ensure AgingDays < 1
                    df.loc[idx, 'Due Date'] = reference_date + timedelta(days=1)
        
    except FileNotFoundError:
        # CSV file doesn't exist, skip adjustment
        pass
    except Exception as e:
        # Log error but don't break the process
        print(f"Warning: Error applying current bucket adjustment: {e}")
        pass
    
    return df

def blank_payment_terms_for_credit_payment(df):
    """Only change Payment_Terms: set to blank where Document Type is CREDIT MEMO or PAYMENT. Exception: retain Payment_Terms if they contain 'IS' or 'BID'. Do not modify Document Type."""
    if df is None or df.empty:
        return df
    pay_col = 'Payment_Terms' if 'Payment_Terms' in df.columns else ('Payment Terms Code' if 'Payment Terms Code' in df.columns else None)
    doc_col = None
    for c in ['DOCUMENT TYPE', 'Document Type', 'DocumentType', 'DOCUMENT_TYPE']:
        if c in df.columns:
            doc_col = c
            break
    if pay_col and doc_col:
        # Read-only use of doc_col to build mask; only write to pay_col
        doc_vals = df[doc_col].astype(str).str.strip().str.upper()
        doc_mask = doc_vals.isin(['CREDIT MEMO', 'PAYMENT'])
        # Exception: do not blank if Payment_Terms contains "IS" or "BID"
        pay_vals = df[pay_col].astype(str).str.upper()
        retain_mask = pay_vals.str.contains('IS', na=False) | pay_vals.str.contains('BID', na=False)
        mask = doc_mask & ~retain_mask
        if mask.any():
            df = df.copy()
            df.loc[mask, pay_col] = ''  # Only Payment_Terms is changed; Document Type is never modified
    return df

def update_calculations(df):
    df = df.copy()
    df = blank_payment_terms_for_credit_payment(df)
    # Ensure AsOfDate and Due Date are datetime
    df['AsOfDate'] = pd.to_datetime(df['AsOfDate'], errors='coerce')
    df['Due Date'] = pd.to_datetime(df['Due Date'], errors='coerce')
    df['Posting Date'] = pd.to_datetime(df['Posting Date'], errors='coerce')
    df['Payment_Terms_Numeric'] = df['Payment_Terms'].str.extract(r'(\d+)').astype(float).fillna(0).astype(int)
    
    # Use the first non-null AsOfDate as the reference date, with fallback
    reference_date = df['AsOfDate'].dropna().iloc[0] if not df['AsOfDate'].dropna().empty else None # can replace None into session_tate.date_to_str
    
    # Calculate adjusted Due Date with error handling for ADD Days
    def safe_add_days(row):
        try:
            add_days = int(float(row['ADD Days']) if pd.notna(row['ADD Days']) else 0)
        except (ValueError, TypeError):
            add_days = 0
            
        # Early return if Due Date or Posting Date is NaN
        if pd.isna(row['Due Date']) or pd.isna(row['Posting Date']):
            return row['Due Date']

        # Do not apply add_days if Balance Due / Remaining Balance / BalanceDue is negative
        for bal_col in ['Balance Due', 'Remaining Balance', 'BalanceDue']:
            if bal_col in row.index:
                try:
                    bal = pd.to_numeric(row[bal_col], errors='coerce')
                    if pd.notna(bal) and bal < 0:
                        return row['Due Date']
                except (ValueError, TypeError):
                    pass
                break

        # Compute days_diff for this row only (scalar)
        # Removing days_diff = (row['Due Date'] - row['Posting Date']).days
        pay_term = int(row['Payment_Terms_Numeric']) if pd.notna(row['Payment_Terms_Numeric']) else 0
        # Return original Due Date if difference > 31 days
        if pay_term > 45 or add_days == 0:
            return row['Due Date']

        # Otherwise, add add_days to Due Date
        return row['Due Date'] + timedelta(days=add_days)
    
    df['Due Date'] = df.apply(safe_add_days, axis=1)
    # Exclude HOSP000058, HOSP000526 from ADD Days reset - they keep ADD Days in main data_editor (exemption applies ONLY in modal display_merged)
    cust_col = 'Customer No_' if 'Customer No_' in df.columns else None
    mask = (df['Payment_Terms_Numeric'] > 45) & (df['ADD Days'] > 0)
    if cust_col:
        mask = mask & (~df['Customer No_'].astype(str).str.strip().isin(['HOSP000058', 'HOSP000526']))
    remark_text = "Due Date not adjusted, Payment Terms is >= 60 days"
    df.loc[mask, 'Remarks'] = df['Remarks'].str.replace(remark_text, '', regex=False).str.replace(' |  | ',' | ', regex=False)  # Remove existing remark if present
    df.loc[mask, 'Remarks'] = df.loc[mask, 'Remarks'].fillna('') + " | " + remark_text  
    df.loc[mask, 'ADD Days'] = 0  # Reset ADD Days to 0 if not adjusted  
    # Reset ADD Days to 0 for rows with negative Balance Due / Remaining Balance / BalanceDue
    bal_col_reset = next((c for c in ['Balance Due', 'Remaining Balance', 'BalanceDue'] if c in df.columns), None)
    if bal_col_reset:
        df.loc[pd.to_numeric(df[bal_col_reset], errors='coerce').fillna(0) < 0, 'ADD Days'] = 0
    
    # Apply special condition: Keep specific customers in Current bucket by adjusting Due Date
    # This reads from Current_Bucket_Customers.csv and adjusts Due Date to ensure AgingDays < 1
    df = apply_current_bucket_adjustment(df, reference_date)
    
    # Calculate days difference
    df['AgingDays'] = df.apply(
        lambda row: (reference_date - row['Due Date']).days if not pd.isna(row['Due Date']) else None,
        axis=1
    )
    
    # Calculate aging buckets
    df['Current'] = df.apply(lambda row: row['Balance Due'] if row['AgingDays'] is not None and row['AgingDays'] < 1 else None, axis=1)
    df['Days_1_to_30'] = df.apply(lambda row: row['Balance Due'] if row['AgingDays'] is not None and 1 <= row['AgingDays'] <= 30 else None, axis=1)
    df['Days_31_to_60'] = df.apply(lambda row: row['Balance Due'] if row['AgingDays'] is not None and 31 <= row['AgingDays'] <= 60 else None, axis=1)
    df['Days_61_to_90'] = df.apply(lambda row: row['Balance Due'] if row['AgingDays'] is not None and 61 <= row['AgingDays'] <= 90 else None, axis=1)
    df['Over_91_Days'] = df.apply(lambda row: row['Balance Due'] if row['AgingDays'] is not None and row['AgingDays'] >= 91 else None, axis=1)
    df['Total Target'] = df.apply(lambda row: row['Balance Due'] if row['AgingDays'] is not None and row['AgingDays'] > 0 else 0, axis=1)
    # df['DETAIL_ITEM_CODE'] = df['DETAIL_ITEM_CODE'].str.strip(";")
    # df['DETAIL_ITEM_NAME'] = df['DETAIL_ITEM_NAME'].str.strip(";")
    
    # Define all expected columns (input + calculated)
    expected_columns = ['ADD Days', 'Customer No_', 'Posting Date', 'Name', 'City', 'AREA', 'AREA_NAME',
                       'Gen_ Bus_ Posting Group', 'DOCUMENT TYPE', 'Payment_Terms',
                       'Document No_', 'External Document No_','Description', 'Entry No_', 'Closed by Entry No_', 'Customer Posting Group',
                       'Due Date', 'AsOfDate', 'AgingDays', 'Balance Due', 'Current',
                       'Days_1_to_30', 'Days_31_to_60', 'Days_61_to_90', 'Over_91_Days', 'Total Target',
                       'ITEM CODE', 'PRODUCT', 'DEPT CODE', 'PMR',
                       'PMR_NAME', 'DSM', 'DSM_NAME', 'SR', 'SR_NAME', 'SR2', 'SR_CODE2', 'CR', 'CR_NAME', 'DSS', 'DSS_NAME', 'NSM',
                       'NSM_NAME', 'PM', 'PM_NAME', 'CUSTCAT', 'CUSTCAT_NAME', 'PG', 'PG_NAME','Remarks','Original Due Date']
    
    # Preserve DSS2_Name and Category columns if they exist (for display_df6_view)
    # This ensures backward compatibility with other processes that don't have these columns
    optional_columns = ['DSS2_Name', 'Category']
    for col in optional_columns:
        if col in df.columns:
            if col not in expected_columns:
                expected_columns.append(col)
    
    # Filter to only expected columns that exist in the DataFrame
    # This prevents errors if a column in expected_columns doesn't exist
    available_columns = [col for col in expected_columns if col in df.columns]
    return df[available_columns]  # Filter to only expected columns that exist

def update_calculations_1(df):
    """
    Exclusive function for the A/R Report code block (lines 3532-3676).
    This is a copy of update_calculations() to avoid conflicts with other processes.
    """
    df = df.copy()
    df = blank_payment_terms_for_credit_payment(df)
    # Ensure AsOfDate and Due Date are datetime
    df['AsOfDate'] = pd.to_datetime(df['AsOfDate'], errors='coerce')
    df['Due Date'] = pd.to_datetime(df['Due Date'], errors='coerce')
    df['Posting Date'] = pd.to_datetime(df['Posting Date'], errors='coerce')
    df['Payment_Terms_Numeric'] = df['Payment_Terms'].str.extract(r'(\d+)').astype(float).fillna(0).astype(int)
    
    # Use the first non-null AsOfDate as the reference date, with fallback
    reference_date = df['AsOfDate'].dropna().iloc[0] if not df['AsOfDate'].dropna().empty else None # can replace None into session_tate.date_to_str
    
    # Calculate adjusted Due Date with error handling for ADD Days
    def safe_add_days(row):
        try:
            add_days = int(float(row['ADD Days']) if pd.notna(row['ADD Days']) else 0)
        except (ValueError, TypeError):
            add_days = 0
            
        # Early return if Due Date or Posting Date is NaN
        if pd.isna(row['Due Date']) or pd.isna(row['Posting Date']):
            return row['Due Date']

        # Do not apply add_days if Balance Due / Remaining Balance / BalanceDue is negative
        for bal_col in ['Balance Due', 'Remaining Balance', 'BalanceDue']:
            if bal_col in row.index:
                try:
                    bal = pd.to_numeric(row[bal_col], errors='coerce')
                    if pd.notna(bal) and bal < 0:
                        return row['Due Date']
                except (ValueError, TypeError):
                    pass
                break

        # Compute days_diff for this row only (scalar)
        # Removing days_diff = (row['Due Date'] - row['Posting Date']).days
        pay_term = int(row['Payment_Terms_Numeric']) if pd.notna(row['Payment_Terms_Numeric']) else 0
        # Return original Due Date if difference > 31 days
        if pay_term > 45 or add_days == 0:
            return row['Due Date']

        # Otherwise, add add_days to Due Date
        return row['Due Date'] + timedelta(days=add_days)
    
    df['Due Date'] = df.apply(safe_add_days, axis=1)
    # Exclude HOSP000058, HOSP000526 from ADD Days reset - they keep ADD Days (exemption applies ONLY in modal display_merged)
    cust_col = 'Customer No_' if 'Customer No_' in df.columns else None
    mask = (df['Payment_Terms_Numeric'] > 45) & (df['ADD Days'] > 0)
    if cust_col:
        mask = mask & (~df['Customer No_'].astype(str).str.strip().isin(['HOSP000058', 'HOSP000526']))
    remark_text = "Due Date not adjusted, Payment Terms is >= 60 days"
    df.loc[mask, 'Remarks'] = df['Remarks'].str.replace(remark_text, '', regex=False).str.replace(' |  | ',' | ', regex=False)  # Remove existing remark if present
    df.loc[mask, 'Remarks'] = df.loc[mask, 'Remarks'].fillna('') + " | " + remark_text  
    df.loc[mask, 'ADD Days'] = 0  # Reset ADD Days to 0 if not adjusted  
    # Reset ADD Days to 0 for rows with negative Balance Due / Remaining Balance / BalanceDue
    bal_col_reset_1 = next((c for c in ['Balance Due', 'Remaining Balance', 'BalanceDue'] if c in df.columns), None)
    if bal_col_reset_1:
        df.loc[pd.to_numeric(df[bal_col_reset_1], errors='coerce').fillna(0) < 0, 'ADD Days'] = 0
    
    # Calculate days difference
    df['AgingDays'] = df.apply(
        lambda row: (reference_date - row['Due Date']).days if not pd.isna(row['Due Date']) else None,
        axis=1
    )
    
    # Calculate aging buckets
    df['Current'] = df.apply(lambda row: row['Balance Due'] if row['AgingDays'] is not None and row['AgingDays'] < 1 else None, axis=1)
    df['Days_1_to_30'] = df.apply(lambda row: row['Balance Due'] if row['AgingDays'] is not None and 1 <= row['AgingDays'] <= 30 else None, axis=1)
    df['Days_31_to_60'] = df.apply(lambda row: row['Balance Due'] if row['AgingDays'] is not None and 31 <= row['AgingDays'] <= 60 else None, axis=1)
    df['Days_61_to_90'] = df.apply(lambda row: row['Balance Due'] if row['AgingDays'] is not None and 61 <= row['AgingDays'] <= 90 else None, axis=1)
    df['Over_91_Days'] = df.apply(lambda row: row['Balance Due'] if row['AgingDays'] is not None and row['AgingDays'] >= 91 else None, axis=1)
    df['Total Target'] = df.apply(lambda row: row['Balance Due'] if row['AgingDays'] is not None and row['AgingDays'] > 0 else 0, axis=1)
    # df['DETAIL_ITEM_CODE'] = df['DETAIL_ITEM_CODE'].str.strip(";")
    # df['DETAIL_ITEM_NAME'] = df['DETAIL_ITEM_NAME'].str.strip(";")
    
    # Define all expected columns (input + calculated)
    expected_columns = ['ADD Days', 'Customer No_', 'Posting Date', 'Name', 'City', 'AREA', 'AREA_NAME',
                       'Gen_ Bus_ Posting Group', 'DOCUMENT TYPE', 'Payment_Terms',
                       'Document No_', 'External Document No_','Description', 'Entry No_', 'Closed by Entry No_', 'Customer Posting Group',
                       'Due Date', 'AsOfDate', 'AgingDays', 'Balance Due', 'Current',
                       'Days_1_to_30', 'Days_31_to_60', 'Days_61_to_90', 'Over_91_Days', 'Total Target',
                       'ITEM CODE', 'PRODUCT', 'DEPT CODE', 'PMR',
                       'PMR_NAME', 'DSM', 'DSM_NAME', 'SR', 'SR_NAME', 'SR2', 'SR_CODE2', 'CR', 'CR_NAME', 'DSS', 'DSS_NAME', 'NSM',
                       'NSM_NAME', 'PM', 'PM_NAME', 'CUSTCAT', 'CUSTCAT_NAME', 'PG', 'PG_NAME','Remarks','Original Due Date']
    
    # Preserve DSS2_Name and Category columns if they exist (for display_df6_view)
    # This ensures backward compatibility with other processes that don't have these columns
    optional_columns = ['DSS2_Name', 'Category']
    for col in optional_columns:
        if col in df.columns:
            if col not in expected_columns:
                expected_columns.append(col)
    
    # Filter to only expected columns that exist in the DataFrame
    # This prevents errors if a column in expected_columns doesn't exist
    available_columns = [col for col in expected_columns if col in df.columns]
    return df[available_columns]  # Filter to only expected columns that exist

def _is_apply_global_true(val):
    """Check if Apply Global column value is True."""
    if pd.isna(val):
        return False
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ('true', '1', 'yes')


def apply_re_tag_history_dss_to_df(df):
    """
    Apply DSS Re-tag History (re_tag_history_dss.csv) to the dataframe before displaying.
    Updates BOTH DSS_NAME and DSS for matching rows.
    - Apply Global = False: match (Entry No_, Original DSS_Name) -> apply to that specific row only.
    - Apply Global = True: match Original DSS_Name only -> apply to ALL rows with that Original DSS_Name.
    """
    if df is None or df.empty:
        return df
    history_file = 're_tag_history_dss.csv'
    try:
        history_df = pd.read_csv(history_file)
    except FileNotFoundError:
        return df
    except Exception:
        return df
    orig_col_check = 'Original DSS_Name' if 'Original DSS_Name' in history_df.columns else 'Original DSS_NAME'
    dss_name_col_check = 'DSS_Name' if 'DSS_Name' in history_df.columns else 'DSS_NAME'
    if history_df.empty or orig_col_check not in history_df.columns or dss_name_col_check not in history_df.columns:
        return df
    # Normalize column names (DSS_Name vs DSS_NAME in history)
    dss_name_col = dss_name_col_check
    orig_col = 'Original DSS_Name' if 'Original DSS_Name' in history_df.columns else 'Original DSS_NAME'
    lookup_dss_specific = {}
    lookup_dss_code_specific = {}
    lookup_dss_global = {}
    lookup_dss_code_global = {}
    has_dss_code = 'DSS' in history_df.columns
    has_apply_global = 'Apply Global' in history_df.columns
    for _, row in history_df.iterrows():
        entry_no = row.get('Entry No_')
        orig = str(row.get(orig_col, '')).strip() if pd.notna(row.get(orig_col)) else ''
        new_dss_name = str(row.get(dss_name_col, '')).strip() if pd.notna(row.get(dss_name_col)) else ''
        apply_global = _is_apply_global_true(row.get('Apply Global')) if has_apply_global else False
        if not new_dss_name:
            continue
        if apply_global:
            lookup_dss_global[orig] = new_dss_name
            if has_dss_code and pd.notna(row.get('DSS')):
                lookup_dss_code_global[orig] = row.get('DSS')
        else:
            if pd.notna(entry_no):
                key = (str(entry_no).strip(), orig)
                lookup_dss_specific[key] = new_dss_name
                if has_dss_code and pd.notna(row.get('DSS')):
                    lookup_dss_code_specific[key] = row.get('DSS')
    if not lookup_dss_specific and not lookup_dss_global:
        return df
    # Data rows (exclude grand total)
    data_len = len(df) - 1 if len(df) > 1 else len(df)
    data_rows = df.iloc[:data_len]
    dss_col = 'DSS' if 'DSS' in df.columns else None
    dss_name_col_df = 'DSS_NAME' if 'DSS_NAME' in df.columns else None
    if not dss_name_col_df:
        return df
    # Build DSS_NAME -> DSS mapping from data
    dss_name_to_dss_map = {}
    if dss_col:
        for dss_name in data_rows[dss_name_col_df].dropna().unique():
            dss_name_str = str(dss_name).strip()
            dss_rows = data_rows[data_rows[dss_name_col_df].astype(str).str.strip() == dss_name_str]
            codes = dss_rows[dss_col].dropna()
            if len(codes) > 0:
                mode_vals = codes.mode()
                dss_name_to_dss_map[dss_name_str] = mode_vals.iloc[0] if len(mode_vals) > 0 else codes.iloc[0]
    try:
        hist = pd.read_csv('re_tag_history_dss.csv')
        if not hist.empty and dss_name_col in hist.columns and 'DSS' in hist.columns:
            for _, row in hist.iterrows():
                s = str(row[dss_name_col]).strip() if pd.notna(row.get(dss_name_col)) else ''
                c = str(row['DSS']).strip() if pd.notna(row.get('DSS')) else ''
                if s and c:
                    dss_name_to_dss_map[s] = c
    except FileNotFoundError:
        pass
    entry_col = 'Entry No_' if 'Entry No_' in df.columns else None
    if not entry_col:
        return df
    df = df.copy()
    for idx in data_rows.index:
        entry_no = df.loc[idx, entry_col]
        current_dss_name = str(df.loc[idx, dss_name_col_df]).strip() if pd.notna(df.loc[idx, dss_name_col_df]) else ''
        new_dss_name = None
        new_dss_code = None
        key_specific = (str(entry_no).strip() if pd.notna(entry_no) else '', current_dss_name)
        if key_specific in lookup_dss_specific:
            new_dss_name = lookup_dss_specific[key_specific]
            if key_specific in lookup_dss_code_specific:
                new_dss_code = lookup_dss_code_specific[key_specific]
        elif current_dss_name in lookup_dss_global:
            new_dss_name = lookup_dss_global[current_dss_name]
            if current_dss_name in lookup_dss_code_global:
                new_dss_code = lookup_dss_code_global[current_dss_name]
        if new_dss_name:
            df.loc[idx, dss_name_col_df] = new_dss_name
            if dss_col:
                if new_dss_code is not None:
                    df.loc[idx, dss_col] = new_dss_code
                elif new_dss_name in dss_name_to_dss_map:
                    df.loc[idx, dss_col] = dss_name_to_dss_map[new_dss_name]
    return df


def apply_re_tag_history_to_df(df):
    """
    Apply Re-tag History (re_tag_history.csv) to the dataframe before displaying in data_editor.
    Updates BOTH SR2 and SR_Code2 for matching rows.
    - Apply Global = False: match (Entry No_, Original SR2) -> apply to that specific row only.
    - Apply Global = True: match Original SR2 only -> apply to ALL rows with that Original SR2.
    """
    if df is None or df.empty:
        return df
    history_file = 're_tag_history.csv'
    try:
        history_df = pd.read_csv(history_file)
    except FileNotFoundError:
        return df
    except Exception:
        return df
    if history_df.empty or 'Original SR2' not in history_df.columns or 'SR2' not in history_df.columns:
        return df
    # Build two lookups:
    # 1. Specific: (Entry No_, Original SR2) for Apply Global = False (Task 4: DEPT CODE not used)
    # 2. Global: (Original SR2, DEPT CODE filter) for Apply Global = True (Task 3: DEPT CODE as additional condition)
    #    - DEPT CODE blank/empty: apply to ALL rows with that Original SR2
    #    - DEPT CODE set: apply only when main table DEPT CODE matches
    lookup_sr2_specific = {}
    lookup_sr_code2_specific = {}
    global_rules = []  # list of (orig_sr2, dept_filter, new_sr2, new_sr_code2); dept_filter '' means apply to all
    has_sr_code2 = 'SR_Code2' in history_df.columns
    has_apply_global = 'Apply Global' in history_df.columns
    has_dept_code = 'DEPT CODE' in history_df.columns
    for _, row in history_df.iterrows():
        entry_no = row.get('Entry No_')
        orig = str(row.get('Original SR2', '')).strip() if pd.notna(row.get('Original SR2')) else ''
        new_sr2 = str(row.get('SR2', '')).strip() if pd.notna(row.get('SR2')) else ''
        apply_global = _is_apply_global_true(row.get('Apply Global')) if has_apply_global else False
        dept_filter = str(row.get('DEPT CODE', '')).strip() if has_dept_code and pd.notna(row.get('DEPT CODE')) else ''
        if isinstance(dept_filter, str) and dept_filter.lower() in ('blank', 'none'):
            dept_filter = ''
        if not new_sr2:
            continue
        if apply_global:
            new_sr_code2 = row.get('SR_Code2') if has_sr_code2 and pd.notna(row.get('SR_Code2')) else None
            global_rules.append((orig, dept_filter, new_sr2, new_sr_code2))
        else:
            if pd.notna(entry_no):
                key = (str(entry_no).strip(), orig)
                lookup_sr2_specific[key] = new_sr2
                if has_sr_code2 and pd.notna(row.get('SR_Code2')):
                    lookup_sr_code2_specific[key] = row.get('SR_Code2')
    if not lookup_sr2_specific and not global_rules:
        return df
    # Data rows (exclude grand total)
    data_len = len(df) - 1 if len(df) > 1 else len(df)
    data_rows = df.iloc[:data_len]
    sr_code2_col = 'SR_CODE2' if 'SR_CODE2' in df.columns else ('SR_Code2' if 'SR_Code2' in df.columns else None)
    sr2_to_sr_code2_map = {}
    if sr_code2_col:
        sr2_to_sr_code2_list = {}
        cr_name_col = next((c for c in ['CR_NAME', 'CR_Name'] if c in data_rows.columns), None)
        cr_col = next((c for c in ['CR', 'CR_CODE'] if c in data_rows.columns), None)
        for sr2 in data_rows['SR2'].dropna().unique():
            sr2_str = str(sr2).strip()
            sr2_rows = data_rows[data_rows['SR2'].astype(str).str.strip() == sr2_str]
            codes = []
            for _, row in sr2_rows.iterrows():
                cr_val = str(row[cr_col]).strip() if cr_col and cr_col in row.index and pd.notna(row.get(cr_col)) else ''
                cr_name_val = str(row.get(cr_name_col, '')).strip() if cr_name_col and cr_name_col in row.index and pd.notna(row.get(cr_name_col)) else ''
                same_person = (cr_name_val == sr2_str or
                    (len(sr2_str.split()) >= 2 and len(cr_name_val.split()) >= 2 and sr2_str.split()[:2] == cr_name_val.split()[:2]))
                if cr_val and cr_name_val and same_person:
                    codes.append(cr_val)
                else:
                    v = row.get(sr_code2_col)
                    if pd.notna(v) and str(v).strip():
                        codes.append(str(v).strip())
            if codes:
                sr2_to_sr_code2_list.setdefault(sr2_str, []).extend(codes)
        try:
            hist = pd.read_csv('re_tag_history.csv')
            if not hist.empty and 'SR2' in hist.columns and 'SR_Code2' in hist.columns:
                for _, row in hist.iterrows():
                    s = str(row['SR2']).strip() if pd.notna(row['SR2']) else ''
                    c = str(row['SR_Code2']).strip() if pd.notna(row['SR_Code2']) else ''
                    if s and c:
                        sr2_to_sr_code2_list.setdefault(s, []).append(c)
        except FileNotFoundError:
            pass
        for sr2_str, codes in sr2_to_sr_code2_list.items():
            if codes:
                mode_vals = pd.Series(codes).mode()
                sr2_to_sr_code2_map[sr2_str] = mode_vals.iloc[0] if len(mode_vals) > 0 else codes[0]
    entry_col = 'Entry No_' if 'Entry No_' in df.columns else None
    if not entry_col:
        return df
    dept_col_main = next((c for c in ['DEPT CODE', 'dept_code'] if c in df.columns), None)
    df = df.copy()
    for idx in data_rows.index:
        entry_no = df.loc[idx, entry_col]
        current_sr2 = str(df.loc[idx, 'SR2']).strip() if pd.notna(df.loc[idx, 'SR2']) else ''
        main_dept = str(df.loc[idx, dept_col_main]).strip() if dept_col_main and dept_col_main in df.columns and pd.notna(df.loc[idx, dept_col_main]) else ''
        new_sr2 = None
        new_sr_code2 = None
        key_specific = (str(entry_no).strip() if pd.notna(entry_no) else '', current_sr2)
        # 1. Try specific match first (Entry No_, Original SR2) - Task 4: DEPT CODE not used
        if key_specific in lookup_sr2_specific:
            new_sr2 = lookup_sr2_specific[key_specific]
            if key_specific in lookup_sr_code2_specific:
                new_sr_code2 = lookup_sr_code2_specific[key_specific]
        # 2. If no specific match, try global rules (Apply Global=True) - Task 3: DEPT CODE as additional condition
        else:
            # Prefer specific dept match first, then fallback to blank (apply to all)
            matched_specific = None
            matched_blank = None
            for orig, dept_filter, nr2, ncode in global_rules:
                if current_sr2 != orig:
                    continue
                if not dept_filter or dept_filter == '':
                    matched_blank = (nr2, ncode)
                elif main_dept == dept_filter:
                    matched_specific = (nr2, ncode)
                    break
            if matched_specific:
                new_sr2, new_sr_code2 = matched_specific[0], matched_specific[1]
            elif matched_blank:
                new_sr2, new_sr_code2 = matched_blank[0], matched_blank[1]
        if new_sr2:
            df.loc[idx, 'SR2'] = new_sr2
            if sr_code2_col:
                # Head Office always uses ZZZ (override history if wrong)
                if str(new_sr2).strip() == 'Head Office':
                    df.loc[idx, sr_code2_col] = 'ZZZ'
                elif new_sr_code2 is not None:
                    df.loc[idx, sr_code2_col] = new_sr_code2
                elif new_sr2 in sr2_to_sr_code2_map:
                    df.loc[idx, sr_code2_col] = sr2_to_sr_code2_map[new_sr2]
    # Final fix: Head Office must always have SR_Code2=ZZZ (catches apply_category, history, or stale data)
    if sr_code2_col and sr_code2_col in df.columns:
        ho_mask = df['SR2'].astype(str).str.strip() == 'Head Office'
        df.loc[ho_mask, sr_code2_col] = 'ZZZ'
    return df


def _sync_dss_from_dss_name(df):
    """
    Force DSS to match DSS_NAME for every row. When DSS_NAME is selected, update DSS from
    data mapping or re_tag_history_dss.
    """
    if df is None or df.empty:
        return df
    dss_col = 'DSS' if 'DSS' in df.columns else None
    dss_name_col = 'DSS_NAME' if 'DSS_NAME' in df.columns else None
    if not dss_col or not dss_name_col:
        return df
    data_len = len(df) - 1 if len(df) > 1 else len(df)
    data_rows = df.iloc[:data_len]
    dss_name_to_dss_map = {}
    for dss_name in data_rows[dss_name_col].dropna().unique():
        dss_name_str = str(dss_name).strip()
        dss_rows = data_rows[data_rows[dss_name_col].astype(str).str.strip() == dss_name_str]
        codes = dss_rows[dss_col].dropna()
        if len(codes) > 0:
            mode_vals = codes.mode()
            dss_name_to_dss_map[dss_name_str] = mode_vals.iloc[0] if len(mode_vals) > 0 else codes.iloc[0]
    try:
        hist = pd.read_csv('re_tag_history_dss.csv')
        dss_name_hist_col = 'DSS_Name' if 'DSS_Name' in hist.columns else 'DSS_NAME'
        if not hist.empty and dss_name_hist_col in hist.columns and 'DSS' in hist.columns:
            for _, row in hist.iterrows():
                s = str(row[dss_name_hist_col]).strip() if pd.notna(row.get(dss_name_hist_col)) else ''
                c = str(row['DSS']).strip() if pd.notna(row.get('DSS')) else ''
                if s and c:
                    dss_name_to_dss_map[s] = c
    except FileNotFoundError:
        pass
    df = df.copy()
    for idx in data_rows.index:
        dss_name_val = str(df.loc[idx, dss_name_col]).strip() if pd.notna(df.loc[idx, dss_name_col]) else ''
        if not dss_name_val:
            continue
        if dss_name_val in dss_name_to_dss_map:
            df.loc[idx, dss_col] = dss_name_to_dss_map[dss_name_val]
    return df


def _write_dss_edits_to_history(merged_df, edited_indices, prev_state):
    """Write DSS_NAME edits (from fragment workaround) to re_tag_history_dss.csv."""
    if not edited_indices:
        return
    dss_col = 'DSS' if 'DSS' in merged_df.columns else None
    if not dss_col:
        return
    history_cols = ['Entry No_', 'Original DSS_Name', 'DSS_Name', 'DSS', 'As Of Month', 'Apply Global']
    entries = []
    for idx in edited_indices:
        if idx >= len(merged_df) or idx >= len(prev_state):
            continue
        orig = str(prev_state.iloc[idx]['DSS_NAME']).strip() if pd.notna(prev_state.iloc[idx].get('DSS_NAME')) else ''
        new_dss_name = str(merged_df.iloc[idx]['DSS_NAME']).strip() if pd.notna(merged_df.iloc[idx].get('DSS_NAME')) else ''
        new_code = str(merged_df.iloc[idx].get(dss_col, '')).strip() if pd.notna(merged_df.iloc[idx].get(dss_col)) else ''
        if not new_dss_name:
            continue
        entry_no = merged_df.iloc[idx].get('Entry No_', '') if 'Entry No_' in merged_df.columns else ''
        asof = merged_df.iloc[idx].get('AsOfDate', '') if 'AsOfDate' in merged_df.columns else ''
        as_of_month = pd.to_datetime(asof).strftime('%m-%Y') if pd.notna(asof) and asof else ''
        entries.append({'Entry No_': entry_no, 'Original DSS_Name': orig, 'DSS_Name': new_dss_name, 'DSS': new_code, 'As Of Month': as_of_month, 'Apply Global': False})
    if not entries:
        return
    try:
        try:
            existing = pd.read_csv('re_tag_history_dss.csv')
            if 'Apply Global' not in existing.columns:
                existing['Apply Global'] = False
            existing = existing.reindex(columns=history_cols, fill_value='')
        except FileNotFoundError:
            existing = pd.DataFrame(columns=history_cols)
        for e in entries:
            mask = pd.Series(False, index=existing.index)
            if str(e.get('Entry No_', '')).strip() and not existing.empty:
                mask = (existing['Entry No_'].astype(str).str.strip() == str(e.get('Entry No_', '')).strip()) & (existing['Original DSS_Name'].astype(str).str.strip() == str(e.get('Original DSS_Name', '')).strip())
                if not mask.any():
                    mask = existing['Entry No_'].astype(str).str.strip() == str(e.get('Entry No_', '')).strip()
            if mask.any():
                idx_match = existing[mask].index[0]
                for c in history_cols:
                    existing.loc[idx_match, c] = e.get(c, '')
            else:
                existing = pd.concat([existing, pd.DataFrame([e])], ignore_index=True)
        existing.to_csv('re_tag_history_dss.csv', index=False)
    except Exception as ex:
        logging.warning(f"Could not write re_tag_history_dss.csv: {ex}")


def _write_sr2_edits_to_history(merged_df, edited_indices, prev_state):
    """Write SR2 edits (from fragment workaround) to re_tag_history.csv."""
    if not edited_indices:
        return
    sr_code2_col = 'SR_CODE2' if 'SR_CODE2' in merged_df.columns else ('SR_Code2' if 'SR_Code2' in merged_df.columns else None)
    if not sr_code2_col:
        return
    history_cols = ['Entry No_', 'Original SR2', 'SR2', 'SR_Code2', 'As Of Month', 'Apply Global', 'DEPT CODE']
    entries = []
    dept_col_src = next((c for c in ['DEPT CODE', 'dept_code'] if c in merged_df.columns), None)
    for idx in edited_indices:
        if idx >= len(merged_df) or idx >= len(prev_state):
            continue
        orig = str(prev_state.iloc[idx]['SR2']).strip() if pd.notna(prev_state.iloc[idx].get('SR2')) else ''
        new_sr2 = str(merged_df.iloc[idx]['SR2']).strip() if pd.notna(merged_df.iloc[idx].get('SR2')) else ''
        new_code = str(merged_df.iloc[idx].get(sr_code2_col, '')).strip() if pd.notna(merged_df.iloc[idx].get(sr_code2_col)) else ''
        if not new_sr2:
            continue
        entry_no = merged_df.iloc[idx].get('Entry No_', '') if 'Entry No_' in merged_df.columns else ''
        asof = merged_df.iloc[idx].get('AsOfDate', '') if 'AsOfDate' in merged_df.columns else ''
        as_of_month = pd.to_datetime(asof).strftime('%m-%Y') if pd.notna(asof) and asof else ''
        dept_val = str(merged_df.iloc[idx].get(dept_col_src, '')).strip() if dept_col_src and pd.notna(merged_df.iloc[idx].get(dept_col_src)) else ''
        entries.append({'Entry No_': entry_no, 'Original SR2': orig, 'SR2': new_sr2, 'SR_Code2': new_code or ('ZZZ' if new_sr2 == 'Head Office' else ''), 'As Of Month': as_of_month, 'Apply Global': False, 'DEPT CODE': dept_val})
    if not entries:
        return
    try:
        try:
            existing = pd.read_csv('re_tag_history.csv')
            if 'SR_Code2' not in existing.columns:
                existing['SR_Code2'] = ''
            if 'Apply Global' not in existing.columns:
                existing['Apply Global'] = False
            existing = existing.reindex(columns=history_cols, fill_value='')
        except FileNotFoundError:
            existing = pd.DataFrame(columns=history_cols)
        for e in entries:
            mask = pd.Series(False, index=existing.index)
            if str(e.get('Entry No_', '')).strip() and not existing.empty:
                mask = (existing['Entry No_'].astype(str).str.strip() == str(e.get('Entry No_', '')).strip()) & (existing['Original SR2'].astype(str).str.strip() == str(e.get('Original SR2', '')).strip())
                if not mask.any():
                    mask = existing['Entry No_'].astype(str).str.strip() == str(e.get('Entry No_', '')).strip()
            if mask.any():
                idx_match = existing[mask].index[0]
                for c in history_cols:
                    existing.loc[idx_match, c] = e.get(c, '')
            else:
                existing = pd.concat([existing, pd.DataFrame([e])], ignore_index=True)
        existing.to_csv('re_tag_history.csv', index=False)
    except Exception as ex:
        logging.warning(f"Could not write re_tag_history.csv: {ex}")


def _sync_sr_code2_from_sr2(df):
    """
    Force SR_CODE2 to match SR2 for every row. Use when display_df6_view_state may have
    stale SR_CODE2 (e.g. after callback edit). Prefer row's CR when SR2 matches CR_Name.
    """
    if df is None or df.empty or 'SR2' not in df.columns:
        return df
    sr_code2_col = 'SR_CODE2' if 'SR_CODE2' in df.columns else ('SR_Code2' if 'SR_Code2' in df.columns else None)
    if not sr_code2_col:
        return df
    data_len = len(df) - 1 if len(df) > 1 else len(df)
    data_rows = df.iloc[:data_len]
    cr_name_col = next((c for c in ['CR_NAME', 'CR_Name'] if c in data_rows.columns), None)
    cr_col = next((c for c in ['CR', 'CR_CODE'] if c in data_rows.columns), None)
    # Build SR2 -> SR_CODE2 mapping (same logic as apply_re_tag_history)
    sr2_to_sr_code2_list = {}
    for sr2 in data_rows['SR2'].dropna().unique():
        sr2_str = str(sr2).strip()
        sr2_rows = data_rows[data_rows['SR2'].astype(str).str.strip() == sr2_str]
        codes = []
        for _, row in sr2_rows.iterrows():
            cr_val = str(row[cr_col]).strip() if cr_col and cr_col in row.index and pd.notna(row.get(cr_col)) else ''
            cr_name_val = str(row.get(cr_name_col, '')).strip() if cr_name_col and cr_name_col in row.index and pd.notna(row.get(cr_name_col)) else ''
            same_person = (cr_name_val == sr2_str or
                (len(sr2_str.split()) >= 2 and len(cr_name_val.split()) >= 2 and sr2_str.split()[:2] == cr_name_val.split()[:2]))
            if cr_val and cr_name_val and same_person:
                codes.append(cr_val)
            else:
                v = row.get(sr_code2_col)
                if pd.notna(v) and str(v).strip():
                    codes.append(str(v).strip())
        if codes:
            sr2_to_sr_code2_list.setdefault(sr2_str, []).extend(codes)
    try:
        hist = pd.read_csv('re_tag_history.csv')
        if not hist.empty and 'SR2' in hist.columns and 'SR_Code2' in hist.columns:
            for _, row in hist.iterrows():
                s = str(row['SR2']).strip() if pd.notna(row['SR2']) else ''
                c = str(row['SR_Code2']).strip() if pd.notna(row['SR_Code2']) else ''
                if s and c:
                    sr2_to_sr_code2_list.setdefault(s, []).append(c)
    except FileNotFoundError:
        pass
    sr2_to_sr_code2_map = {}
    for sr2_str, codes in sr2_to_sr_code2_list.items():
        if codes:
            mode_vals = pd.Series(codes).mode()
            sr2_to_sr_code2_map[sr2_str] = mode_vals.iloc[0] if len(mode_vals) > 0 else codes[0]
    if DEBUG_SR_CODE2 and 'Mark Francis Leoncio' in sr2_to_sr_code2_map:
        _sr2_debug_log(f"[_sync_sr_code2] map has Mark Francis Leoncio -> {sr2_to_sr_code2_map.get('Mark Francis Leoncio')}")
    df = df.copy()
    for idx in data_rows.index:
        sr2_val = str(df.loc[idx, 'SR2']).strip() if pd.notna(df.loc[idx, 'SR2']) else ''
        if not sr2_val:
            continue
        if sr2_val == 'Head Office':
            df.loc[idx, sr_code2_col] = 'ZZZ'
            continue
        # Prefer row's CR when SR2 matches CR_Name (same person)
        cr_name_val = str(df.loc[idx].get(cr_name_col, '')).strip() if cr_name_col and cr_name_col in df.columns and pd.notna(df.loc[idx].get(cr_name_col)) else ''
        same_person = (cr_name_val == sr2_val or
            (len(sr2_val.split()) >= 2 and len(cr_name_val.split()) >= 2 and sr2_val.split()[:2] == cr_name_val.split()[:2]))
        if same_person and cr_col and cr_col in df.columns:
            cr_val = df.loc[idx, cr_col]
            if pd.notna(cr_val) and str(cr_val).strip():
                df.loc[idx, sr_code2_col] = str(cr_val).strip()
                if DEBUG_SR_CODE2 and sr2_val == 'Mark Francis Leoncio':
                    _sr2_debug_log(f"[_sync_sr_code2] idx={idx} SR2={sr2_val} set SR_CODE2={cr_val} (from CR, same_person)")
                continue
        if sr2_val in sr2_to_sr_code2_map:
            df.loc[idx, sr_code2_col] = sr2_to_sr_code2_map[sr2_val]
            if DEBUG_SR_CODE2 and sr2_val == 'Mark Francis Leoncio':
                _sr2_debug_log(f"[_sync_sr_code2] idx={idx} SR2={sr2_val} set SR_CODE2={sr2_to_sr_code2_map[sr2_val]} (from map)")
    return df


def df_on_change_sr2():
    """Callback function for SR2 and DSS_NAME data editor changes in display_df6_view."""
    _sr2_debug_log("[CALLBACK] df_on_change_sr2 triggered")
    # Get the current editor state
    if "display_df6_editor" not in st.session_state:
        _sr2_debug_log("[CALLBACK] display_df6_editor not in session_state - RETURN")
        return
    
    state = st.session_state["display_df6_editor"]
    dss_edited_indices = []
    # Handle both formats: dict with edited_rows (older) or DataFrame (newer Streamlit)
    if isinstance(state, dict):
        edited_rows = state.get("edited_rows", {})
        # Extract DSS_NAME edits from edited_rows (dict format doesn't populate dss_edited_indices above)
        for idx, updates in edited_rows.items():
            if isinstance(updates, dict) and 'DSS_NAME' in updates:
                dss_edited_indices.append(idx)
    elif hasattr(state, 'loc') and hasattr(state, 'columns'):
        # State is the edited DataFrame - compute SR2 and DSS_NAME diffs vs display_df6_view_state
        prev = st.session_state.display_df6_view_state
        if 'SR2' not in state.columns or len(state) != len(prev):
            return
        edited_rows = {}
        for idx in range(min(len(state), len(prev))):
            old_val = str(prev.iloc[idx]['SR2']).strip() if pd.notna(prev.iloc[idx].get('SR2')) else ''
            new_val = str(state.iloc[idx]['SR2']).strip() if pd.notna(state.iloc[idx].get('SR2')) else ''
            if old_val != new_val:
                edited_rows[idx] = {'SR2': state.iloc[idx]['SR2']}
        # Compute DSS_NAME diffs
        if 'DSS_NAME' in state.columns and 'DSS_NAME' in prev.columns:
            for idx in range(min(len(state), len(prev))):
                old_dss = str(prev.iloc[idx]['DSS_NAME']).strip() if pd.notna(prev.iloc[idx].get('DSS_NAME')) else ''
                new_dss = str(state.iloc[idx]['DSS_NAME']).strip() if pd.notna(state.iloc[idx].get('DSS_NAME')) else ''
                if old_dss != new_dss:
                    dss_edited_indices.append(idx)
        # Use the widget's dataframe as base (has user's SR2 and DSS_NAME edits)
        edited_df = state.copy()
    else:
        return
    
    if not edited_rows and not dss_edited_indices:
        _sr2_debug_log("[CALLBACK] edited_rows and dss_edited_indices empty - RETURN")
        return
    
    _sr2_debug_log(f"[CALLBACK] edited_rows={edited_rows} dss_edited_indices={dss_edited_indices}")
    # Base dataframe: use widget's edited data when state is DataFrame, else apply edits to display_df6_view_state
    if isinstance(state, dict):
        edited_df = st.session_state.display_df6_view_state.copy()
        # Apply DSS_NAME edits from edited_rows to edited_df (dict format)
        if 'DSS_NAME' in edited_df.columns:
            for idx in dss_edited_indices:
                if idx < len(edited_df) and idx in edited_rows:
                    val = edited_rows[idx].get('DSS_NAME')
                    if val is not None:
                        edited_df.loc[edited_df.index[idx], 'DSS_NAME'] = val
    # else edited_df already set above from state.copy()

    # For DSS_NAME edits: sync DSS from DSS_NAME and write to re_tag_history_dss
    if dss_edited_indices:
        edited_df = _sync_dss_from_dss_name(edited_df)
        prev_for_dss = st.session_state.display_df6_view_state
        _write_dss_edits_to_history(edited_df, dss_edited_indices, prev_for_dss)
    
    # Get unique list of SR2 and SR_Code2 from display_df6_view_state (has re-tag history; excluding grand total)
    _src = st.session_state.display_df6_view_state
    data_rows = _src.iloc[:-1].copy() if len(_src) > 1 else _src.copy()
    
    # Check if SR_Code2 column exists (could be SR_CODE2 or SR_Code2); prefer SR_CODE2 (canonical/source column)
    sr_code2_col = None
    if 'SR_CODE2' in data_rows.columns:
        sr_code2_col = 'SR_CODE2'
    elif 'SR_Code2' in data_rows.columns:
        sr_code2_col = 'SR_Code2'
    _sr2_debug_log(f"[CALLBACK] sr_code2_col={sr_code2_col} | data_rows.columns SR_CODE2={('SR_CODE2' in data_rows.columns)} SR_Code2={('SR_Code2' in data_rows.columns)}")
    
    # Get unique mapping of SR2 to SR_Code2 (combine data_rows + re_tag_history, use mode for frequency)
    # Prefer CR over SR_Code2 when SR2 matches CR_Name (same person) - CR is often the canonical person code
    if sr_code2_col:
        sr2_to_sr_code2_list = {}  # sr2 -> list of sr_code2 values for mode
        # 1. From data_rows: collect SR_Code2 per SR2; prefer CR when SR2 == CR_Name (same person, e.g. Mark Francis Leoncio -> SR060)
        data_rows_sr2_norm = data_rows['SR2'].astype(str).str.strip()
        cr_name_col = next((c for c in ['CR_NAME', 'CR_Name'] if c in data_rows.columns), None)
        cr_col = next((c for c in ['CR', 'CR_CODE'] if c in data_rows.columns), None)
        for sr2 in data_rows['SR2'].dropna().unique():
            sr2_str = str(sr2).strip()
            mask = data_rows_sr2_norm == sr2_str
            sub = data_rows.loc[mask]
            codes = []
            for _, row in sub.iterrows():
                # When SR2 matches CR_Name (same person), prefer SR_Code/CR - canonical person code (e.g. Mark Francis Leoncio -> SR060)
                cr_val = str(row[cr_col]).strip() if cr_col and cr_col in row.index and pd.notna(row.get(cr_col)) else ''
                sr_code_val = next((str(row[c]).strip() for c in ['SR_Code', 'SR_CODE', 'SCR'] if c in row.index and pd.notna(row.get(c)) and str(row.get(c)).strip()), '')
                sr2_code_val = str(row.get(sr_code2_col, '')).strip() if pd.notna(row.get(sr_code2_col)) else ''
                cr_name_val = str(row.get(cr_name_col, '')).strip() if cr_name_col and cr_name_col in row.index and pd.notna(row.get(cr_name_col)) else ''
                # Same person: exact match or first 2 words match (handles "Mark Francis Leor" vs "Mark Francis Leoncio")
                same_person = (cr_name_val == sr2_str or
                    (len(sr2_str.split()) >= 2 and len(cr_name_val.split()) >= 2 and sr2_str.split()[:2] == cr_name_val.split()[:2]))
                if same_person and cr_name_val:
                    if sr_code_val:
                        codes.append(sr_code_val)  # SR_Code often has correct person code (SR060)
                    elif cr_val:
                        codes.append(cr_val)
                    elif sr2_code_val:
                        codes.append(sr2_code_val)
                elif sr2_code_val:
                    codes.append(sr2_code_val)
            if codes:
                sr2_to_sr_code2_list.setdefault(sr2_str, []).extend(codes)
        # 2. From re_tag_history: add SR_Code2 per SR2 to the pool
        try:
            history_df_map = pd.read_csv('re_tag_history.csv')
            if not history_df_map.empty and 'SR2' in history_df_map.columns and 'SR_Code2' in history_df_map.columns:
                for _, row in history_df_map.iterrows():
                    sr2_val = str(row['SR2']).strip() if pd.notna(row['SR2']) else ''
                    code_val = str(row['SR_Code2']).strip() if pd.notna(row['SR_Code2']) else ''
                    if sr2_val and code_val:
                        sr2_to_sr_code2_list.setdefault(sr2_val, []).append(code_val)
        except FileNotFoundError:
            pass
        # 3. For each SR2, take mode of combined SR_Code2 list
        sr2_to_sr_code2_map = {}
        for sr2_str, codes in sr2_to_sr_code2_list.items():
            if codes:
                mode_vals = pd.Series(codes).mode()
                sr2_to_sr_code2_map[sr2_str] = mode_vals.iloc[0] if len(mode_vals) > 0 else codes[0]
        # Get list of valid SR2 names for validation (include re-tag history targets so edits to those values are accepted)
        valid_sr2_names = set(str(sr2).strip() for sr2 in data_rows['SR2'].dropna().unique())
    else:
        sr2_to_sr_code2_map = {}
        valid_sr2_names = set(str(sr2).strip() for sr2 in data_rows['SR2'].dropna().unique())
    cr_name_col = next((c for c in ['CR_NAME', 'CR_Name'] if c in data_rows.columns), None)
    cr_col = next((c for c in ['CR', 'CR_CODE'] if c in data_rows.columns), None)
    # Add unique [SR2 + CR_Name] to valid list: CR_Name values so user can type CR_Name (e.g. "Mark Francis Leoncio") as valid target
    cr_name_to_sr2_map = {}
    cr_name_to_sr_code2_map = {}
    if cr_name_col:
        # Build unique [SR2 + CR_Name] mapping: CR_Name -> (SR2, SR_Code2/CR). Use mode for SR2 when CR_Name has multiple SR2.
        grp = data_rows.groupby(cr_name_col, dropna=False)
        for cr_name_val, sub in grp:
            cr_name_str = str(cr_name_val).strip() if pd.notna(cr_name_val) and str(cr_name_val).strip() else ''
            if not cr_name_str or cr_name_str.lower() == 'nan':
                continue
            valid_sr2_names.add(cr_name_str)
            sr2_mode = sub['SR2'].mode()
            sr2_val = str(sr2_mode.iloc[0]).strip() if len(sr2_mode) > 0 and pd.notna(sr2_mode.iloc[0]) else ''
            cr_name_to_sr2_map[cr_name_str] = sr2_val
            # Prefer CR over SR_Code2 for CR_Name mapping (CR is canonical person code, e.g. Mark Francis Leoncio -> SR060)
            if cr_col and cr_col in sub.columns:
                code_vals = sub[cr_col].dropna()
            elif sr_code2_col and sr_code2_col in sub.columns:
                code_vals = sub[sr_code2_col].dropna()
            else:
                code_vals = pd.Series(dtype=object)
            if len(code_vals) > 0:
                code_mode = code_vals.mode()
                code_val = str(code_mode.iloc[0]).strip() if len(code_mode) > 0 else ''
                if code_val:
                    cr_name_to_sr_code2_map[cr_name_str] = code_val
    # Add SR2 values from re_tag_history so edits to values that exist only in history are accepted
    try:
        history_df_for_valid = pd.read_csv('re_tag_history.csv')
        if not history_df_for_valid.empty and 'SR2' in history_df_for_valid.columns:
            for sr2 in history_df_for_valid['SR2'].dropna().unique():
                valid_sr2_names.add(str(sr2).strip())
    except FileNotFoundError:
        pass
    
    # Track warnings for invalid SR2 values (no match -> revert)
    warnings = []
    # Track which rows had SR2 edited by the user (only those that matched and were applied)
    user_edited_sr2_indices = set()
    # Track successful re-tags for history log: list of dicts with Entry No_, Original SR2, SR2, As Of Month
    re_tag_history_entries = []
    
    # Apply the edits from the data editor
    for index, updates in edited_rows.items():
        # Skip grand total row if it exists
        if index >= len(edited_df) - 1 and len(st.session_state.display_df6_view) > 1:
            continue
            
        for key, value in updates.items():
            if key == 'SR2':
                # Original SR2 before user edit (for revert and history)
                original_sr2 = str(edited_df.loc[index, 'SR2']).strip() if pd.notna(edited_df.loc[index, 'SR2']) else ''
                # Normalize the new SR2 value
                new_sr2 = str(value).strip() if pd.notna(value) else ''
                
                # Fallback: if new_sr2 doesn't match any existing SR2 names, revert to original and fix SR_Code2
                if new_sr2 and new_sr2 not in valid_sr2_names:
                    edited_df.loc[index, 'SR2'] = original_sr2
                    # Also update SR_Code2 to match the reverted original_sr2 (e.g. Head Office -> ZZZ)
                    if sr_code2_col and original_sr2:
                        revert_code = cr_name_to_sr_code2_map.get(original_sr2)
                        if revert_code is not None:
                            edited_df.loc[index, sr_code2_col] = revert_code
                        elif original_sr2 in sr2_to_sr_code2_map:
                            edited_df.loc[index, sr_code2_col] = sr2_to_sr_code2_map[original_sr2]
                        else:
                            matching = data_rows[data_rows['SR2'].astype(str).str.strip() == original_sr2]
                            if len(matching) > 0:
                                codes = matching[sr_code2_col].dropna()
                                if len(codes) > 0:
                                    mode_vals = codes.mode()
                                    edited_df.loc[index, sr_code2_col] = mode_vals.iloc[0] if len(mode_vals) > 0 else codes.iloc[0]
                    warnings.append(f"Row {index + 1}: SR2 '{new_sr2}' doesn't match any existing SR2 names. Reverted to '{original_sr2}'.")
                    continue
                
                # Keep user's selection as SR2 - do NOT resolve through cr_name_to_sr2_map (that maps
                # CR_Name->SR2 from data and would overwrite "Mark Francis Leoncio" with "Head Office")
                resolved_sr2 = new_sr2
                resolved_sr_code2 = cr_name_to_sr_code2_map.get(new_sr2) or sr2_to_sr_code2_map.get(new_sr2)
                
                # Match found: apply new SR2 and auto-update SR_Code2
                user_edited_sr2_indices.add(index)
                edited_df.loc[index, 'SR2'] = resolved_sr2
                
                # Auto-update SR_Code2: prefer row's SR_Code/CR when CR_Name matches new SR2 (canonical per-row)
                row_cr_name = ''
                if cr_name_col and cr_name_col in edited_df.columns and pd.notna(edited_df.loc[index, cr_name_col]):
                    row_cr_name = str(edited_df.loc[index, cr_name_col]).strip()
                row_same_person = (row_cr_name == resolved_sr2 or
                    (len(resolved_sr2.split()) >= 2 and len(row_cr_name.split()) >= 2 and resolved_sr2.split()[:2] == row_cr_name.split()[:2]))
                row_code = None
                if row_same_person and row_cr_name:
                    for code_col in ['SR_Code', 'SR_CODE', 'SCR', cr_col]:
                        if code_col and code_col in edited_df.columns:
                            v = edited_df.loc[index, code_col]
                            if pd.notna(v) and str(v).strip():
                                row_code = str(v).strip()
                                break
                
                # Apply SR_Code2 (row code > CR_Name map > sr2 map > data_rows)
                before_val = str(edited_df.loc[index, sr_code2_col] if sr_code2_col else '') if sr_code2_col and sr_code2_col in edited_df.columns else 'N/A'
                applied_code = None
                if sr_code2_col and row_code:
                    edited_df.loc[index, sr_code2_col] = row_code
                    applied_code = f"row_code={row_code}"
                elif sr_code2_col and resolved_sr_code2:
                    edited_df.loc[index, sr_code2_col] = resolved_sr_code2
                    applied_code = f"resolved_sr_code2={resolved_sr_code2}"
                elif sr_code2_col and resolved_sr2 in sr2_to_sr_code2_map:
                    edited_df.loc[index, sr_code2_col] = sr2_to_sr_code2_map[resolved_sr2]
                    applied_code = f"sr2_map={sr2_to_sr_code2_map[resolved_sr2]}"
                elif sr_code2_col and resolved_sr2:
                    matching_rows = data_rows[data_rows['SR2'].astype(str).str.strip() == resolved_sr2]
                    if len(matching_rows) > 0:
                        sr_code2_values = matching_rows[sr_code2_col].dropna()
                        if len(sr_code2_values) > 0:
                            mode_values = sr_code2_values.mode()
                            edited_df.loc[index, sr_code2_col] = mode_values.iloc[0] if len(mode_values) > 0 else sr_code2_values.iloc[0]
                            applied_code = f"data_rows_mode={mode_values.iloc[0] if len(mode_values) > 0 else sr_code2_values.iloc[0]}"
                after_val = str(edited_df.loc[index, sr_code2_col]) if sr_code2_col and sr_code2_col in edited_df.columns else 'N/A'
                _sr2_debug_log(f"[CALLBACK] row={index} orig_sr2={original_sr2} new_sr2={new_sr2} resolved_sr2={resolved_sr2} | SR_CODE2 before={before_val} after={after_val} | applied={applied_code or 'NONE'}")
                
                # Collect for re-tag history: use ONLY lookup/mapping (Unique Set of SR2), NOT main table (old code)
                entry_no = edited_df.loc[index, 'Entry No_'] if 'Entry No_' in edited_df.columns else ''
                if resolved_sr2 and str(resolved_sr2).strip() == 'Head Office':
                    new_sr_code2 = 'ZZZ'  # Always ZZZ for Head Office in history
                else:
                    new_sr_code2 = str((cr_name_to_sr_code2_map.get(new_sr2) or
                        sr2_to_sr_code2_map.get(new_sr2, '')) or '').strip()
                asof = edited_df.loc[index, 'AsOfDate'] if 'AsOfDate' in edited_df.columns else None
                if asof is not None and pd.notna(asof):
                    try:
                        as_of_month = pd.to_datetime(asof).strftime('%m-%Y')
                    except (ValueError, TypeError):
                        as_of_month = str(asof)
                else:
                    as_of_month = edited_df.loc[index, 'As Of Month'] if 'As Of Month' in edited_df.columns else ''
                re_tag_history_entries.append({
                    'Entry No_': entry_no,
                    'Original SR2': original_sr2,
                    'SR2': resolved_sr2,
                    'SR_Code2': new_sr_code2,
                    'As Of Month': as_of_month
                })
    
    # Store the user-edited SR2 values before applying category function
    user_edited_sr2_values = {}
    for idx in user_edited_sr2_indices:
        if idx < len(edited_df):
            user_edited_sr2_values[idx] = edited_df.iloc[idx]['SR2']
    
    # Update the session state with edited dataframe
    st.session_state.display_df6_view_state = edited_df.copy()
    
    # Exclude grand total row before applying category function
    if len(edited_df) > 1:
        data_rows_to_update = edited_df.iloc[:-1].copy()
        grand_total = edited_df.iloc[-1:].copy()
    else:
        data_rows_to_update = edited_df.copy()
        grand_total = pd.DataFrame()
    
    # Apply category function to correct Category and DSS2_name
    updated_data_rows = apply_category_to_display_df(data_rows_to_update)
    
    # Preserve user-edited SR2 values, except where specific conditions require changes
    # (e.g., negative balance forces 'Head Office')
    for idx, user_sr2 in user_edited_sr2_values.items():
        if idx < len(updated_data_rows):
            # Only override user edit if balance is negative (forces 'Head Office')
            balance_due = updated_data_rows.iloc[idx].get('Balance Due', 0)
            try:
                balance_numeric = float(balance_due)
                if balance_numeric >= 0:
                    # Preserve user's SR2 edit for non-negative balances
                    updated_data_rows.iloc[idx, updated_data_rows.columns.get_loc('SR2')] = user_sr2
                    # Also preserve SR_Code2 from edited_df for user-edited rows
                    if sr_code2_col and sr_code2_col in updated_data_rows.columns and idx < len(edited_df):
                        updated_data_rows.iloc[idx, updated_data_rows.columns.get_loc(sr_code2_col)] = edited_df.iloc[idx][sr_code2_col]
                # If balance is negative, let the function's result stand (forces 'Head Office')
            except (ValueError, TypeError):
                # If balance can't be converted, preserve user edit
                updated_data_rows.iloc[idx, updated_data_rows.columns.get_loc('SR2')] = user_sr2
                if sr_code2_col and sr_code2_col in updated_data_rows.columns and idx < len(edited_df):
                    updated_data_rows.iloc[idx, updated_data_rows.columns.get_loc(sr_code2_col)] = edited_df.iloc[idx][sr_code2_col]
    
    # When SR2 is Head Office, ensure SR_Code2 is ZZZ (apply_category doesn't set SR_Code2)
    if sr_code2_col and sr_code2_col in updated_data_rows.columns:
        ho_mask = updated_data_rows['SR2'].astype(str).str.strip() == 'Head Office'
        updated_data_rows.loc[ho_mask, sr_code2_col] = 'ZZZ'
    
    # Reattach grand total if it existed
    if not grand_total.empty:
        final_df = pd.concat([updated_data_rows, grand_total], ignore_index=True)
    else:
        final_df = updated_data_rows
    
    # Update the main session state
    st.session_state.display_df6_view = final_df.copy()
    _sr2_debug_log(f"[CALLBACK] Set display_df6_view | sample SR_CODE2={final_df[sr_code2_col].iloc[:5].tolist() if sr_code2_col and sr_code2_col in final_df.columns else 'N/A'}")
    
    # Update the editor state to reflect the changes
    st.session_state.display_df6_view_state = final_df.copy()
    _sr2_debug_log("[CALLBACK] Set display_df6_view_state. About to st.rerun().")
    
    # Propagate SR2 and DSS_NAME edits to display_df6s (Collection Performance tab) so all state/displays stay in sync
    if 'display_df6s' in st.session_state and not st.session_state.display_df6s.empty and 'Entry No_' in final_df.columns and 'Entry No_' in st.session_state.display_df6s.columns:
        df6s = st.session_state.display_df6s.copy()
        entry_to_sr2 = final_df.set_index('Entry No_')['SR2'].to_dict()
        sr_code2_col_src = 'SR_CODE2' if 'SR_CODE2' in final_df.columns else ('SR_Code2' if 'SR_Code2' in final_df.columns else None)
        sr_code2_col_dst = 'SR_CODE2' if 'SR_CODE2' in df6s.columns else ('SR_Code2' if 'SR_Code2' in df6s.columns else None)
        entry_to_sr_code2 = final_df.set_index('Entry No_')[sr_code2_col_src].to_dict() if sr_code2_col_src else {}
        entry_to_dss_name = final_df.set_index('Entry No_')['DSS_NAME'].to_dict() if 'DSS_NAME' in final_df.columns else {}
        entry_to_dss = final_df.set_index('Entry No_')['DSS'].to_dict() if 'DSS' in final_df.columns else {}
        for idx, row in df6s.iterrows():
            en = row.get('Entry No_')
            en_str = str(en).strip() if pd.notna(en) else ''
            if en_str in entry_to_sr2:
                df6s.at[idx, 'SR2'] = entry_to_sr2[en_str]
                if sr_code2_col_dst and en_str in entry_to_sr_code2 and pd.notna(entry_to_sr_code2.get(en_str)):
                    df6s.at[idx, sr_code2_col_dst] = entry_to_sr_code2[en_str]
            if en_str in entry_to_dss_name and 'DSS_NAME' in df6s.columns:
                df6s.at[idx, 'DSS_NAME'] = entry_to_dss_name[en_str]
                if en_str in entry_to_dss and 'DSS' in df6s.columns:
                    df6s.at[idx, 'DSS'] = entry_to_dss[en_str]
        st.session_state.display_df6s = df6s
    
    # Log successful re-tags to re_tag_history.csv (include SR_Code2 so mapping can use it directly)
    # If same Entry No_ exists in history -> update that row; else append
    if re_tag_history_entries:
        try:
            history_cols = ['Entry No_', 'Original SR2', 'SR2', 'SR_Code2', 'As Of Month', 'Apply Global']
            history_file = 're_tag_history.csv'
            try:
                existing = pd.read_csv(history_file)
                if 'SR_Code2' not in existing.columns:
                    existing['SR_Code2'] = ''
                if 'Apply Global' not in existing.columns:
                    existing['Apply Global'] = False
                existing = existing.reindex(columns=history_cols, fill_value='')
                existing['Apply Global'] = existing['Apply Global'].apply(
                    lambda x: str(x).strip().lower() in ('true', '1', 'yes') if pd.notna(x) and str(x).strip() else False
                )
            except FileNotFoundError:
                existing = pd.DataFrame(columns=history_cols)
            # Process each new entry: update if Entry No_ exists, else append
            for entry in re_tag_history_entries:
                entry_no = str(entry.get('Entry No_', '')).strip() if pd.notna(entry.get('Entry No_')) else ''
                orig_sr2 = str(entry.get('Original SR2', '')).strip() if pd.notna(entry.get('Original SR2')) else ''
                new_sr2 = str(entry.get('SR2', '')).strip() if pd.notna(entry.get('SR2')) else ''
                new_sr_code2 = str(entry.get('SR_Code2', '')).strip() if pd.notna(entry.get('SR_Code2')) else ''
                as_of_month = str(entry.get('As Of Month', '')).strip() if pd.notna(entry.get('As Of Month')) else ''
                # Match by (Entry No_, Original SR2) for precision; fallback to Entry No_ only when entry_no present
                mask = pd.Series(False, index=existing.index)
                if entry_no and not existing.empty:
                    mask = (existing['Entry No_'].astype(str).str.strip() == entry_no) & (existing['Original SR2'].astype(str).str.strip() == orig_sr2)
                    if not mask.any():
                        mask = existing['Entry No_'].astype(str).str.strip() == entry_no
                if mask.any():
                    # Update existing row(s) - take first match
                    idx = existing[mask].index[0]
                    existing.loc[idx, 'SR2'] = new_sr2
                    existing.loc[idx, 'SR_Code2'] = new_sr_code2
                    existing.loc[idx, 'As Of Month'] = as_of_month
                    existing.loc[idx, 'Apply Global'] = False  # Table edits are always specific
                else:
                    # Append new row
                    new_row = pd.DataFrame([{
                        'Entry No_': entry.get('Entry No_', ''),
                        'Original SR2': orig_sr2,
                        'SR2': new_sr2,
                        'SR_Code2': new_sr_code2,
                        'As Of Month': as_of_month,
                        'Apply Global': False
                    }])
                    existing = pd.concat([existing, new_row], ignore_index=True)
            existing.to_csv(history_file, index=False)
        except Exception as e:
            logging.warning(f"Could not write re_tag_history.csv: {e}")
    
    # Display warnings if any (revert / no-match messages)
    if warnings:
        for warning in warnings:
            st.warning(warning)

    # Force full script rerun so fragment runs in full context and SR_CODE2 updates persist
    # (Fragment-only reruns may not reflect callback's session state updates reliably)
    st.rerun()

def df_on_change():
    # Get the current editor state
    state = st.session_state["performance_editor"]
    edited_rows = state.get("edited_rows", {})

    # Make a copy of the current DataFrame to work with
    edited_df = st.session_state.performance_df_state.copy()

    # Apply the edits from the data editor
    for index, updates in edited_rows.items():
        scr_value = edited_df.iloc[index]["SCR"]
        mask = edited_df["SCR"] == scr_value
        for key, value in updates.items():
            edited_df.loc[mask, key] = value

    # Convert all numeric columns to numeric (handle any string input)
    numeric_cols = ['Total Collected Amount (PHP)', 'Collection Perf Rate (%)', 'TOTAL TARGET', 'RETURNS', 'Other Adjustments', 'NET TARGET']
    for col in numeric_cols:
        edited_df[col] = pd.to_numeric(
            edited_df[col].astype(str).replace(r'[^\d.-]', '', regex=True),
            errors='coerce'
        ).fillna(0.0)

    # Compute "Collection Perf Rate (%)" for all rows 
    # edited_df['NET TARGET'] = np.where(True,
    #     (edited_df['TARGET'] + edited_df["RETURNS"] + edited_df["Other Adjustments"]).round(2),0.0)
    edited_df['NET TARGET'] = (edited_df[['TOTAL TARGET', 'RETURNS', 'Other Adjustments']].fillna(0).sum(axis=1)).round(2)    
    edited_df["Collection Perf Rate (%)"] = np.where(
        edited_df["NET TARGET"] != 0,
        (edited_df["Total Collected Amount (PHP)"] / edited_df["TOTAL TARGET"] * 100).round(2),
        0.0
    )

    # Format numeric columns for display
    for col in numeric_cols:
        edited_df[col] = edited_df[col].apply(
            lambda x: f"{float(x):,.2f}" if pd.notna(x) else "0.00"
        )

    # Update the session state
    st.session_state.performance_df_state = edited_df
    # st.rerun() # Force a rerun to update the display

# Callback function for data editor changes
def df_on_change2():
    edited_rows = st.session_state.data_editor_df6s.get("edited_rows", {})
    if edited_rows:
        for row_idx, updates in edited_rows.items():
            if row_idx < len(st.session_state.display_df6s) - 1:  # Skip grand totals row
                for col, value in updates.items():
                    st.session_state.display_df6s.loc[row_idx, col] = value
        # Recalculate only data rows and append grand totals
        data_rows = st.session_state.display_df6s.iloc[:-1].copy()
        updated_df = update_calculations(data_rows)
        # Reattach grand total row
        grand_total = st.session_state.display_df6s.iloc[-1:].copy()
        st.session_state.display_df6s = pd.concat([updated_df, grand_total], ignore_index=True)

def get_image_base64(path):
    with open(path, "rb") as image_file:
        encoded_string = base64.b64encode(image_file.read()).decode()
    return f"data:image/png;base64,{encoded_string}"

# Function to authenticate user and retrieve access level
def authenticate_user(username, password, domain="INNOGEN-PHARMA"):
    if not username:  # Skip if no username provided
        return False
    
    logger.info(f"Login attempt for user: {username}")
    
    # Check for lockout
    if username in st.session_state.failed_attempts:
        attempts = st.session_state.failed_attempts[username]
        if time.time() - attempts['timestamp'] < st.session_state.lockout_duration and attempts['count'] >= st.session_state.max_attempts:
            logger.warning(f"User '{username}' is locked out due to too many failed attempts")
            st.error(f"Account locked due to too many failed attempts. Try again in {st.session_state.lockout_duration // 60} minutes.")
            return False
        # Reset if lockout expired
        elif time.time() - attempts['timestamp'] >= st.session_state.lockout_duration:
            del st.session_state.failed_attempts[username]
    
    server = Server('192.168.16.1', port=389, get_info=ALL)
    user_dn = f"{domain}\\{username}"  # NTLM format

    try:
        conn = Connection(
            server,
            user=user_dn,
            password=password,
            authentication=NTLM,
            auto_bind=True
        )
        if conn.bind():
            # Search for user to get group membership
            search_base = "DC=INNOGEN-PHARMA,DC=COM"  # Adjust to your domain's base DN
            search_filter = f"(sAMAccountName={username})"
            conn.search(
                search_base=search_base,
                search_filter=search_filter,
                search_scope=SUBTREE,
                attributes=['memberOf']
            )
            if conn.entries:
                groups = conn.entries[0].memberOf.values
                # Determine access level based on group membership
                if any("Admins" in group for group in groups):
                    access_level = "Admin"
                elif any("Managers" in group for group in groups):
                    access_level = "Manager"
                else:
                    access_level = "User"
                st.session_state.authenticated = True
                st.session_state.username = username
                st.session_state.access_level = access_level
                logger.info(f"Successful login for user '{username}'. Access level: {access_level}")
                logger.debug(f"Session state after authentication: {st.session_state}")  # Debug level for sensitive state
                conn.unbind()  # Clean up connection
                
                # Reset attempts on success
                if username in st.session_state.failed_attempts:
                    del st.session_state.failed_attempts[username]
                return True
            else:
                logger.warning(f"User '{username}' not found in LDAP directory")
                st.error("User not found in LDAP directory.")
                conn.unbind()
        else:
            logger.warning(f"Bind failed for user '{username}' - invalid credentials")
            st.error("Bind failed, check credentials.")
            conn.unbind()
    except Exception as e:
        logger.error(f"Authentication error for user '{username}': {str(e)}")
        st.error(f"Authentication error: {str(e)}")
    
    # Track failure (for bind failure, user not found, or exception)
    if username not in st.session_state.failed_attempts:
        st.session_state.failed_attempts[username] = {'count': 0, 'timestamp': time.time()}
    st.session_state.failed_attempts[username]['count'] += 1
    st.session_state.failed_attempts[username]['timestamp'] = time.time()  # Update timestamp
    failed_count = st.session_state.failed_attempts[username]['count']
    logger.warning(f"Failed login attempt for '{username}' (total attempts: {failed_count})")
    if failed_count >= st.session_state.max_attempts:
        st.warning("Too many failed attempts. Account locked for 30 minutes.")
    
    return False

# Login form
def login_form():
    # Clear all cache when landing on login page
    st.cache_data.clear()  # Clears all @st.cache_data caches
    st.cache_resource.clear()  # Clears all @st.cache_resource caches
    
    logger.info("Rendering login form")
    with st.container():
        # Apply custom CSS to center the title and style the form elements
        st.markdown(
            """
            <style>
            /* Center the title */
            .stApp [data-testid="stMarkdownContainer"] h1 {
                text-align: center;
            }
            /* Ensure input fields and button take full width of the column */
            .stTextInput > div > div > input {
                width: 100% !important;
            }
            .stButton > button {
                width: 100% !important;
            }
            /* Remove border from the column (previously styled as card-like) */
            .stApp [data-testid="stVerticalBlock"] > div {
                padding: 10px; /* Keep padding for spacing */
                border: 1; /* Remove border */
                box-shadow: 0; /* Remove shadow */
                border-radius: 5; /* Reset border-radius */
            }
            </style>
            """,
            unsafe_allow_html=True
        )
        
        image_base64 = get_image_base64('InnoGen-Pharmaceuticals-Inc_Logo.png')
        st.markdown(
            f'<img src="{image_base64}" width="200" style="display: block; margin: 0 auto;">',
            unsafe_allow_html=True
        )         
              
        st.markdown("""
            <style>
                /* Isolate styles within a custom container */
                .login-container {
                    width: 100%;
                    text-align: center;
                }

                /* Style the title with high specificity */
                .login-container .login-title {
                    color: #6b3fa0 !important; /* Ensure color applies */
                    font-weight: bold;
                    font-size: 2rem !important; /* Base font size */
                    margin: 2rem 0 !important;
                }

                /* Responsive font sizes for different screen widths */
                @media screen and (max-width: 1024px) {
                    .login-container .login-title {
                        font-size: 1.6rem !important; /* Shrink for large tablets/desktops */
                    }
                }

                @media screen and (max-width: 768px) {
                    .login-container .login-title {
                        font-size: 1.3rem !important; /* Shrink for tablets */
                    }
                }

                @media screen and (max-width: 480px) {
                    .login-container .login-title {
                        font-size: 1.2rem !important; /* Shrink for mobile */
                    }
                }
            </style>

            <div class="login-container">
                <h1 class="login-title">Login to Collection Report System</h1>
            </div>
        """, unsafe_allow_html=True) 

        # Use columns to control the width and centering of the form
        col1, col2, col3 = st.columns([1, 2, 1])  # Middle column takes more space
        with col2:
            with st.form("login_form", clear_on_submit=True):
                username = st.text_input("Username")
                password = st.text_input("Password", type="password")
                submit = st.form_submit_button("Login")
                
                if submit:
                    logger.info("Login form submitted")
                    if authenticate_user(username, password):
                        st.success("Login successful!")
                        logger.info("Login successful, forcing rerun")
                        st.rerun()
                    else:
                        st.error("Invalid username or password, please try again.")
                        logger.warning("Login failed")
                                             
# Function to add month-year share columns with proper numeric conversion
def add_month_year_share_columns(df, share_column='%_share_ar', reference_column='detaildate'):
    result_df = df.copy()
    month_year_cols = [col for col in result_df.columns[result_df.columns.get_loc(reference_column) + 1:]
                       if re.match(r'^\d{2}-[a-z]{3}$', col) and not col.endswith('%_')]
    
    for col in month_year_cols:
        result_df[col] = result_df[col].astype(str)
        result_df[col] = result_df[col].apply(lambda x: x.split('-')[1] if x.startswith('-') and x.count('-') > 1 else x)
        result_df[col] = result_df[col].str.replace(r'[^\d.-]', '', regex=True)
        result_df[col] = pd.to_numeric(result_df[col], errors='coerce')
        share_col = f"%_{col}"
        values = result_df[share_column] * result_df[col]
        result_df.insert(result_df.columns.get_loc(col) + 1, share_col, values.where(values.notna()))
    
    return result_df

# Initialize session state
print("Starting session state initialization")
if 'authenticated' not in st.session_state:
    st.session_state.authenticated = False
if 'username' not in st.session_state:
    st.session_state.username = None
if 'access_level' not in st.session_state:
    st.session_state.access_level = None
if 'result_df' not in st.session_state:
    st.session_state.result_df = None
    st.session_state.result_df3 = None
if 'selected_dsm' not in st.session_state:
    st.session_state.selected_dsm = 'All'
if 'selected_dss' not in st.session_state:
    st.session_state.selected_dss = 'All'
    
print("Session state initialized successfully")

# Main app logic
def main_app():
    print("Entering main_app")
    st.markdown('<a id="report-generator"></a>', unsafe_allow_html=True)
    # Check if user is authenticated and has valid username and access level
    if not st.session_state.authenticated or st.session_state.username is None or st.session_state.access_level is None:
        print("Session invalid in main_app")
        st.error("Session invalid. Please log in again.")
        # Reset session state
        st.session_state.authenticated = False
        st.session_state.username = None
        st.session_state.access_level = None
        return
    
    # scroll_top()
    date_range_fragment()
    
    # Form ONLY for the submit button (no widgets inside to enable real-time updates)
    with st.form("date_form"):
        submit_button = st.form_submit_button("Generate Report")
 
    if submit_button:
        # For Progress Bar and Status Text %
        mcol1, mcol2 = st.columns([1, 15])
        with mcol1:
            status_text = st.empty()
        with mcol2:
            progress_bar = st.progress(0) 
        st.session_state.bar_text = "Generating Report..."       
        with st.spinner("Processing... Please Wait..."):
            try:
                date_from = st.session_state.date_from
                date_to = st.session_state.date_to
                
                host = _get_config_value("db", "host", "DB_HOST")
                us_r = _get_config_value("db", "user", "DB_USER")
                pas_ = _get_config_value("db", "password", "DB_PASSWORD")
                data = _get_config_value("db", "database", "DB_DATABASE", default="RXTracking", required=False)
                driver = _get_config_value("db", "driver", "DB_DRIVER", default="ODBC Driver 18 for SQL Server", required=False)
                trust_cert = _get_config_value("db", "trust_server_certificate", "DB_TRUST_SERVER_CERTIFICATE", default="yes", required=False)
                pas_ = urllib.parse.quote_plus(pas_)
                
                # # For Windows Compatibility
                # engine = create_engine(f"mssql+pyodbc://{us_r}:{pas_}@{host}/{data}?driver=SQL Server")
                
                # # For Cross Platform Compatibility
                engine = create_engine(
                    f"mssql+pyodbc://{us_r}:{pas_}@{host}/{data}?driver={urllib.parse.quote_plus(driver)}&TrustServerCertificate={trust_cert}"
                )
                
                sproc1 = "sp_final_direct_sales_4collection"
                sproc2 = "sp_bc365_cust_ledger_pivot_optimize"
                sproc3 = "sp_GetRecords_Incentive_Bal"
                sproc4 = "sp_GetRecords_Incentive_Bal_lines"
                sproc5 = "sp_rebate_accruals"
                sproc6 = "sp_AR_Collection_Details"
                sproc7_cc = "sp_AR_CombinedCollectionReport" # To be use by Sir Patrick for manual checking
                sproc7 = "sp_bc365_cust_ledger_pivot_optimize_AsOf"
                sproc8 = "sp_VisMin_Data" 
                sproc8a = "sp_AR_AddDays" 
                sproc9 = "sp_bc365_Cust_Ledger_Entry"
                sproc10 = "sp_bc365_G_L Entry"
                
                date_from_str = date_from.strftime('%Y-%m-%d')
                date_to_str = date_to.strftime('%Y-%m-%d')

# DATABASE
#>>>____________________________________________________________________________________________________________________________________________#
                # with st.spinner("SQL DATABASE - Processing stored procedures..."):
                        
                total_steps = 10  # Total number of dataframes (DF1 to DF9)
                current_step = 0
                
                # Initialize progress bar and status text once

#>>>____________________________________________________________________________________________________________________________________________#                
                with engine.connect() as connection:
                    # DF1 - Processing...
                    current_step += 1
                    with mcol1:
                        status_text.text(f"{int((current_step / total_steps) * 100)}%")
                    with mcol2:
                        progress_bar.progress(int((current_step / total_steps) * 100))   
                    query1 = text(f"EXEC [dbo].[{sproc1}] '{date_from_str}', '{date_to_str}'")
                    df1 = pd.read_sql(query1, connection)

                    if df1.empty or df1 is None:
                        print('df1.empty')
                        # print("Direct Sales - Collection data is invalid or empty. Please choose a different start date.")
                        # st.error("Direct Sales - Collection data is invalid or empty. Please choose a different start date.")
                        # return
                    
                    df1.columns = df1.columns.str.lower()
                    required_columns = ['vlookup','sell_to_customer_no', 'customer_name', 'inv_dr_date', 'no_', 'inv_dr_no', 
                                        'sales_channel', 'payment_terms', 'dept', 'dept_code', 'pm', 'pmr', 'dsm', 'cr', 'sr', 
                                        'net_sales_less_rud_vat_disc', 'gross_ar']
                    required_columns = [col.lower() for col in required_columns]
                    
                    df1 = df1[df1['no_'] != ''][required_columns]
                    # df1 = df1[required_columns]
                    
                    group_cols = [col for col in required_columns if col not in ['net_sales_less_rud_vat_disc', 'gross_ar']]
                    # Group by all columns except numeric ones, sum net_sales_less_rud_vat_disc and gross_ar, count inv_dr_no
                    df1 = df1.groupby(group_cols).agg({
                        'net_sales_less_rud_vat_disc': 'sum',
                        'gross_ar': 'sum'
                    }).reset_index()
                    
                    df1['total_gross_ar'] = df1.groupby('vlookup')['gross_ar'].transform('sum')
                    df1['%_share_ar'] = df1['gross_ar'] / df1['total_gross_ar']
                    
                    # with st.spinner("DF2 - Processing..."):
                    current_step += 1
                    with mcol1:
                        status_text.text(f"{int((current_step / total_steps) * 100)}%")
                    with mcol2:
                        progress_bar.progress(int((current_step / total_steps) * 100))   
                    query2 = text(f"EXEC [dbo].[{sproc2}] '{date_from_str}'")
                    df2 = pd.read_sql(query2, connection)
                    df2.columns = df2.columns.str.lower()
                    
                    # with st.spinner("DF3 - Processing..."):
                    current_step += 1
                    with mcol1:
                        status_text.text(f"{int((current_step / total_steps) * 100)}%")
                    with mcol2:
                        progress_bar.progress(int((current_step / total_steps) * 100)) 
                    query3 = text(f"EXEC [dbo].[{sproc3}] '{date_from_str}', '{date_to_str}'")
                    df3 = pd.read_sql(query3, connection)                
                    df3 = df3[['no_','pmr','pmr_code','%_perf','inv_month','total_incentive','remaining_incentive']]
                    
                    # with st.spinner("DF4 - Processing..."):
                    current_step += 1
                    with mcol1:
                        status_text.text(f"{int((current_step / total_steps) * 100)}%")
                    with mcol2:
                        progress_bar.progress(int((current_step / total_steps) * 100)) 
                    query4 = text(f"EXEC [dbo].[{sproc4}] '{date_from_str}', '{date_to_str}'")
                    df4 = pd.read_sql(query4, connection) 
                    df4 = df4.drop(columns=['ib_no','no_'], errors='ignore')               
                    # df4 = df4[['pmr','pmr_code','%_perf','inv_month','total_incentive','remaining_incentive']]

                    # with st.spinner("DF5 - Processing..."):
                    current_step += 1
                    with mcol1:
                        status_text.text(f"{int((current_step / total_steps) * 100)}%")
                    with mcol2:
                        progress_bar.progress(int((current_step / total_steps) * 100))   
                    query5 = text(f"EXEC [dbo].[{sproc5}]")
                    df5 = pd.read_sql(query5, connection) 
                    
                    # with st.spinner("DF6 - Processing..."):
                    current_step += 1
                    mcol1, col2 = st.columns([2, 3])  # Adjust column widths as needed (e.g., 2:3 ratio)
                    with mcol1:
                        status_text.text(f"{int((current_step / total_steps) * 100)}%")
                    with mcol2:
                        progress_bar.progress(int((current_step / total_steps) * 100))  
                    query6 = text(f"EXEC [dbo].[{sproc6}] '{date_to_str}'")
                    df6 = pd.read_sql(query6, connection) 
                    df6['Original Due Date'] = df6['Due Date']
                    df6['Remarks'] = ''
                    
                    df6['SR2'] = df6['SR2'].str.strip().replace('', np.nan).mask(
                    (df6['SR2'].isna()) & (df6['CR_NAME'].notna()), df6['CR_NAME'])
                    
                    df6['SR_CODE2'] = df6['SR_CODE2'].str.strip().replace('', np.nan).mask(
                        (df6['SR_CODE2'].isna()) & (df6['CR'].notna()), df6['CR'])
                    
                    # Additional condition: Set SR2 and SR_CODE2 when Balance Due < 0
                    df6['SR2'] = df6['SR2'].mask(df6['Balance Due'] < 0, 'Head Office')
                    df6['SR_CODE2'] = df6['SR_CODE2'].mask(df6['Balance Due'] < 0, 'ZZZ')
                    # Force SR_CODE2=ZZZ whenever SR2 is Head Office (catches any source with wrong code)
                    ho_mask = df6['SR2'].astype(str).str.strip() == 'Head Office'
                    if ho_mask.any():
                        df6.loc[ho_mask, 'SR_CODE2'] = 'ZZZ'
                    ### NEED TO DO RETAGGING AFTER MERGE DF7 ###
                    
                    # sproc 7 - AR Combined Collection Report
                    ##############################################################################################################################################################################
                    # Assuming date_from_str and date_to_str are strings like 'YYYY-MM-DD'
                    date_from = datetime.strptime(date_from_str, '%Y-%m-%d')
                    date_to = datetime.strptime(date_to_str, '%Y-%m-%d')

                    # Add one month to both dates
                    new_date_from = date_from + relativedelta(months=1)
                    new_date_to = date_to + relativedelta(months=1,day=31)  # Set to last day of the month

                    # Format back to strings
                    new_date_from_str = new_date_from.strftime('%Y-%m-%d')
                    new_date_to_str = new_date_to.strftime('%Y-%m-%d')

                    # with st.spinner("DF7 - Processing..."):
                    current_step += 1
                    with mcol1:
                        status_text.text(f"{int((current_step / total_steps) * 100)}%")
                    with mcol2:
                        progress_bar.progress(int((current_step / total_steps) * 100))
                    df7 = load_df7(connection, new_date_from_str, new_date_to_str, sproc7)
                    # df7.to_csv("df7_debug.csv", index=False)  # Save df7 to CSV for debugging
                    ##############################################################################################################################################################################
                    # sproc7_cc - Row-Detailed Collection (sp_AR_CombinedCollectionReport)
                    try:
                        query7_cc = text(f"EXEC [dbo].[{sproc7_cc}] '{new_date_from_str}', '{new_date_to_str}'")
                        df7_cc = pd.read_sql(query7_cc, connection)
                    except Exception as e:
                        logger.warning(f"sp_AR_CombinedCollectionReport failed: {e}. Row-Detailed Collection tab will be empty.")
                        df7_cc = pd.DataFrame()
                    ##############################################################################################################################################################################
                    
                    df10 = load_df7a(connection, new_date_from_str, new_date_to_str, sproc10)
                    
                    ##############################################################################################################################################################################
                    
                    # with st.spinner("DF8 - Processing..."):
                    current_step += 1
                    col1, col2 = st.columns([2, 3])  # Adjust column widths as needed (e.g., 2:3 ratio)
                    with mcol1:
                        status_text.text(f"{int((current_step / total_steps) * 100)}%")
                    with mcol2:
                        progress_bar.progress(int((current_step / total_steps) * 100))
                    query8 = text(f"EXEC [dbo].[{sproc8}]")
                    df8 = pd.read_sql(query8, connection)
                    
                    query8a = text(f"EXEC [dbo].[{sproc8a}]")
                    df8a = pd.read_sql(query8a, connection)
                                                                                
                    if sproc8 == "sp_VisMin_Data":
                        df8.rename(columns={'Code': 'DSS_CODE',
                                            'Add': 'ADD_DAYS'}, inplace=True) 
                    
                    # with st.spinner("DF9 - Processing..."):
                    current_step += 1
                    with mcol1:
                        status_text.text(f"{int((current_step / total_steps) * 100)}%")
                    with mcol2:
                        progress_bar.progress(int((current_step / total_steps) * 100)) 
                    df9 = load_df9(connection, sproc9)
                    # df9.to_csv("df9_debug.csv", index=False)  # Save df9 to CSV for debugging   
    #<<<___________________________________________________________________________________________________________________________________________#                
#<<<___________________________________________________________________________________________________________________________________________#
                        
                    df = df1.merge(df2, on='vlookup', how='left')                                
                    df = df.drop(columns=['entryno', 'postingdate', 'documentno', 'documenttypeno',
                                            'documenttype', 'amount', 'customerno', 'reasoncode', 'customername',
                                            'externaldocumentno', 'closedbyentryno', 'blank_date'])
                    df['remaining_balance'] = df['paidunpaid'] * df['%_share_ar']
                    

                    # Suggestion Remarks to identify if WTAX, [WTAX + CVAT], Warranty                
                    # 1% WTAX               [NET VALUE * 1%]
                    # 6% WTAX + CVAT        [NET VALUE * 6%]
                    # 1% Warranty Security  [Gross Value * 1%]
                    
                    # ADDING COLUMN (1% * [net_sales_less_rud_vat_disc]) * df['%_share_ar'] as '1%_wt_ws'
                    # ADDING COLUMN (6% * [gross_ar]) * df['%_share_ar'] as '6%_wt_cv' 
                    
                    # ADDING COLUMN '1%_wt_ws' = 'remaining_balance' AS [suggestion_remarks] = 'WTAX or Warranty Security'
                    # OR '6%_wt_cv' = 'remaining_balance' AS [suggestion_remarks] = 'WTAX + CVAT'
                    # ELSE ''
                    
                    # drop '1%_wt_ws' and '6%_wt_cv'
                    # only remaining column that was added is [suggestion_remarks]

                    df['1%_wt_ws'] = (0.01 * df['net_sales_less_rud_vat_disc']) * df['%_share_ar']
                    df['6%_wt_cv'] = (0.06 * df['gross_ar']) * df['%_share_ar']
                    
                    # ADDING COLUMN suggestion_remarks based on conditions
                    df['suggestion_remarks'] = ''
                    df.loc[df['1%_wt_ws'] == df['remaining_balance'], 'suggestion_remarks'] = 'WTAX or Warranty Security'
                    df.loc[df['6%_wt_cv'] == df['remaining_balance'], 'suggestion_remarks'] = 'WTAX + CVAT'
                    
                    # Drop temporary columns
                    df = df.drop(['1%_wt_ws', '6%_wt_cv'], axis=1)
                    
                    columns = df.columns.tolist()
                    current_index = columns.index('%_share_ar')                
                    columns.insert(current_index + 1, columns.pop(columns.index('suggestion_remarks')))                
                    columns.insert(current_index + 1, columns.pop(columns.index('remaining_balance')))
                    df = df[columns]

                    current_month = datetime.now().strftime('%B').lower()
                    current_month_abbr = datetime.now().strftime('%b').lower()
                    last_column = df.columns[-1]
                    if current_month in last_column or current_month_abbr in last_column:
                        df = df.drop(columns=last_column)

                    result_df = add_month_year_share_columns(df)
                    
                    month_year_cols = [col for col in result_df.columns[result_df.columns.get_loc('detaildate') + 1:]
                                        if re.match(r'^\d{2}-[a-z]{3}$', col) or col.startswith('%_')]
                    for col in month_year_cols:
                        result_df[col] = pd.to_numeric(result_df[col], errors='coerce')
                    
                    st.session_state.result_df = result_df
                    st.session_state.result_df3 = df3
                    st.session_state.result_df4 = df4 # complete details for unpaid incentives
                    st.session_state.result_df5 = df5
                    st.session_state.date_from_str = date_from_str
                    st.session_state.date_to_str = date_to_str
                    st.session_state.result_df6 = df6
                    st.session_state.result_df6a = df6
                    st.session_state.result_df7 = df7
                    st.session_state.result_df7_cc = df7_cc
                    st.session_state.result_df8 = df8
                    st.session_state.result_df8a = df8a
                    st.session_state.result_df9 = df9
                    st.session_state.result_df10 = df10
                    # Progress bar and status text
                    if st.session_state.bar_text is not None:
                        with mcol1:
                            status_text.text("95%")
                        with mcol2:
                            progress_bar.progress(95)  
                                                         
            except Exception as e:
                st.error(f"An error occurred: {str(e)}")
                st.write("Please check the following:")
                st.write("- Database connection (host, credentials, network)")
                st.write("- Required libraries are installed")
                st.write("- SQL Server ODBC driver is configured")

        # Add custom CSS for tabs (force borders to persist after rerun)
        st.markdown("""
        <style>
            .stTabs {
                border: 1px solid #e0e0e0 !important;
                border-radius: 4px;
                overflow: hidden;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            }
            .stTabs [data-baseweb="tab-list"] {
                gap: 2px;
                background-color: #ffffff;
                border-bottom: 1px solid #e6e9ef !important;
                padding: 0 4px;
            }
            .stTabs [data-baseweb="tab"] {
                border-right: 1px solid #e0e0e0;
                border-top: 3px solid transparent;
                border-radius: 4px 4px 0 0;
                padding: 10px 20px;
                transition: all 0.3s ease;
            }
            .stTabs [data-baseweb="tab"]:last-child {
                border-right: none;
            }
            .stTabs [data-baseweb="tab"] [aria-selected="true"],
            .stTabs [aria-selected="true"] {
                background-color: #ffffff !important;
                border-top: 3px solid #6b3fa0 !important;
                border-bottom: 2px solid #6b3fa0 !important;
                border-color: #6b3fa0 !important;
                font-weight: bold;
                color: #6b3fa0;
            }
            .stTabs [data-baseweb="tab"]:has([aria-selected="true"]) {
                border-top: 3px solid #6b3fa0 !important;
                border-bottom: 2px solid #6b3fa0 !important;
            }
            .stTabs [data-baseweb="tab-list"] [aria-selected="true"] {
                border-bottom-color: #6b3fa0 !important;
                box-shadow: inset 0 -2px 0 #6b3fa0 !important;
            }
            .stTabs [data-baseweb="tab"]:hover {
                background-color: #f8f8f8;
                cursor: pointer;
            }
            .stTabs [data-baseweb="tab-panel"] {
                padding: 15px;
                border-top: none;
            }
        </style>
        """, unsafe_allow_html=True)


#++#############################################################################################################################################################################        
#++#############################################################################################################################################################################        
    if st.session_state.result_df is not None and "result_df6" in st.session_state:
        # Inject tab CSS here so it runs on every rerun (fixes disappearing borders after dialog close)
        st.markdown("""
        <style>
            .stTabs { border: 1px solid #e0e0e0 !important; border-radius: 4px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
            .stTabs [data-baseweb="tab-list"] { gap: 2px; background-color: #ffffff; border-bottom: 1px solid #e6e9ef !important; padding: 0 4px; }
            .stTabs [data-baseweb="tab"] { border-right: 1px solid #e0e0e0; border-top: 3px solid transparent; border-bottom: 2px solid transparent; border-radius: 4px 4px 0 0; padding: 10px 20px; }
            .stTabs [data-baseweb="tab"]:last-child { border-right: none; }
            .stTabs [data-baseweb="tab"] [aria-selected="true"], .stTabs [aria-selected="true"] { background-color: #ffffff !important; border-top: 3px solid #6b3fa0 !important; border-bottom: 2px solid #6b3fa0 !important; font-weight: bold; color: #6b3fa0; }
            .stTabs [data-baseweb="tab"]:has([aria-selected="true"]) { border-top: 3px solid #6b3fa0 !important; border-bottom: 2px solid #6b3fa0 !important; }
            .stTabs [data-baseweb="tab"]:hover { background-color: #f8f8f8; cursor: pointer; }
            .stTabs [data-baseweb="tab-panel"] { padding: 15px; border-top: none; }
            .stTabs [data-baseweb="tab"] [aria-selected="true"] { border-color: #6b3fa0 !important; }
            .stTabs [data-baseweb="tab-list"] [aria-selected="true"] { border-bottom-color: #6b3fa0 !important; box-shadow: inset 0 -2px 0 #6b3fa0 !important; }
        </style>
        """, unsafe_allow_html=True)
        # use tabs from here
        # tab1 = main dataframe
        # tab2 = pygwalker
        ###################################
        tabz2, tabz1 = st.tabs(["AR Related Collection","Sales Related Collection"])
        date_from_str = st.session_state.date_from_str
        date_to_str = st.session_state.date_to_str     
        result_df = st.session_state.result_df    
        result_df6 = st.session_state.result_df6
        #######################################################################################
##############################################################################################################################################################################        
        #######################################################################################
               
        with tabz2:
            st.subheader("AR and Collection Data")
            asof_month = (datetime.strptime(date_to_str, '%Y-%m-%d') + relativedelta(months=1,day=31)).strftime('%m-%Y').upper()
            date_to = datetime.strptime(date_to_str, '%Y-%m-%d').strftime('%m-%Y')
            
            stabz1, stabz2 = st.tabs([f"AR Related Data As-of [{date_to}]", f"Collection for the month of [{asof_month}] - (generated base on AR as-of)"])
            #######################################################################################            
            
            with stabz1:
                with st.spinner("A/R Related Report - Processing..."):
                    st.subheader(f"A/R Report As-of [{date_to}]")        
                    # Initialize display_df6_view in session state if it doesn't exist
                    # IMPORTANT: Only initialize if it truly doesn't exist - don't reinitialize if it exists
                    # This preserves user updates (ADD Days, Due Date adjustments)
                    needs_init = False
                    if 'display_df6_view' not in st.session_state:
                        needs_init = True
                    elif st.session_state.display_df6_view.empty:
                        needs_init = True
                    # REMOVED: Don't reinitialize based on row count - this was wiping out user updates
                    # elif len(st.session_state.display_df6_view) != len(result_df6):
                    #     needs_init = True
                    
                    if needs_init:
                        display_df6_view = result_df6.copy()
                        # Force SR_CODE2=ZZZ whenever SR2 is Head Office (fix at load)
                        _sc = 'SR_CODE2' if 'SR_CODE2' in display_df6_view.columns else ('SR_Code2' if 'SR_Code2' in display_df6_view.columns else None)
                        if _sc:
                            _ho = display_df6_view['SR2'].astype(str).str.strip() == 'Head Office'
                            if _ho.any():
                                display_df6_view.loc[_ho, _sc] = 'ZZZ'
                        display_df6_view = display_df6_view.rename(columns={'DETAIL_ITEM_NAME': 'DETAIL_ITEM_NAME (Only 20 Chars Each Name)'})
                        # Ensure ADD Days column exists and is initialized to 0
                        if 'ADD Days' not in display_df6_view.columns:
                            display_df6_view['ADD Days'] = 0
                        else:
                            # Ensure it's numeric and fill any NaN with 0
                            display_df6_view['ADD Days'] = pd.to_numeric(display_df6_view['ADD Days'], errors='coerce').fillna(0).astype(int)
                        
                        # Automatically add +30 days for specific customer names on initial load
                        auto_add_names = [
                            "PLANET PHARMACY (OSMAK)",
                            "PLANET DRUG - MAKATI CITY HEALTH"
                        ]
                        
                        # Process each auto-add customer name
                        # Do not apply add_days to rows with negative Balance Due / Remaining Balance / BalanceDue
                        bal_col_init = next((c for c in ['Balance Due', 'Remaining Balance', 'BalanceDue'] if c in display_df6_view.columns), None)
                        balance_positive_init = pd.to_numeric(display_df6_view[bal_col_init], errors='coerce').fillna(0) >= 0 if bal_col_init else pd.Series([True] * len(display_df6_view), index=display_df6_view.index)
                        for auto_name in auto_add_names:
                            # Normalize Name for matching
                            display_df6_view['Name'] = display_df6_view['Name'].str.strip()
                            matching_mask = (display_df6_view['Name'] == auto_name.strip()) & balance_positive_init
                            
                            if matching_mask.any():
                                # Set ADD Days to 30 for matching rows (positive balance only)
                                display_df6_view.loc[matching_mask, 'ADD Days'] = 30
                                
                                # Ensure Due Date and Original Due Date are datetime
                                display_df6_view['Due Date'] = pd.to_datetime(display_df6_view['Due Date'], errors='coerce')
                                if 'Original Due Date' in display_df6_view.columns:
                                    display_df6_view['Original Due Date'] = pd.to_datetime(display_df6_view['Original Due Date'], errors='coerce')
                                
                                # Adjust Due Date by +30 days for matching rows
                                matching_indices = display_df6_view[matching_mask].index
                                for idx in matching_indices:
                                    if 'Original Due Date' in display_df6_view.columns and pd.notna(display_df6_view.loc[idx, 'Original Due Date']):
                                        base_date = display_df6_view.loc[idx, 'Original Due Date']
                                    else:
                                        base_date = display_df6_view.loc[idx, 'Due Date']
                                    
                                    if pd.notna(base_date):
                                        display_df6_view.loc[idx, 'Due Date'] = base_date + timedelta(days=30)
                        
                        # Call update_calculations to recalculate aging buckets
                        # Exclude grand total if it exists
                        if len(display_df6_view) > 0:
                            # Check if last row is grand total (usually has specific characteristics)
                            data_rows = display_df6_view.iloc[:-1].copy() if len(display_df6_view) > 1 else display_df6_view.copy()
                            grand_total = display_df6_view.iloc[-1:].copy() if len(display_df6_view) > 1 else pd.DataFrame()
                            
                            # Recalculate with update_calculations_1 (exclusive function for this code block)
                            updated_df = update_calculations_1(data_rows)
                            
                            # Restore ADD Days and Due Date for auto-added customers after update_calculations (positive balance only)
                            bal_col_restore = next((c for c in ['Balance Due', 'Remaining Balance', 'BalanceDue'] if c in updated_df.columns), None)
                            balance_positive_restore = pd.to_numeric(updated_df[bal_col_restore], errors='coerce').fillna(0) >= 0 if bal_col_restore else pd.Series([True] * len(updated_df), index=updated_df.index)
                            for auto_name in auto_add_names:
                                updated_df['Name'] = updated_df['Name'].str.strip()
                                selected_mask = (updated_df['Name'] == auto_name.strip()) & balance_positive_restore
                                
                                # Ensure ADD Days stays at 30 (positive balance only)
                                updated_df.loc[selected_mask, 'ADD Days'] = 30
                                
                                # Restore adjusted Due Dates
                                updated_df['Due Date'] = pd.to_datetime(updated_df['Due Date'], errors='coerce')
                                if 'Original Due Date' in updated_df.columns:
                                    updated_df['Original Due Date'] = pd.to_datetime(updated_df['Original Due Date'], errors='coerce')
                                    base_dates = updated_df.loc[selected_mask, 'Original Due Date'].fillna(updated_df.loc[selected_mask, 'Due Date'])
                                else:
                                    base_dates = updated_df.loc[selected_mask, 'Due Date']
                                
                                updated_df.loc[selected_mask, 'Due Date'] = base_dates + timedelta(days=30)
                                
                                # Recalculate aging based on restored Due Date
                                reference_date = updated_df['AsOfDate'].dropna().iloc[0] if not updated_df['AsOfDate'].dropna().empty else None
                                if reference_date:
                                    updated_df.loc[selected_mask, 'AgingDays'] = updated_df.loc[selected_mask].apply(
                                        lambda row: (reference_date - row['Due Date']).days if pd.notna(row['Due Date']) else None,
                                        axis=1
                                    )
                                    # Recalculate aging buckets
                                    updated_df.loc[selected_mask, 'Current'] = updated_df.loc[selected_mask].apply(
                                        lambda row: row['Balance Due'] if row['AgingDays'] is not None and row['AgingDays'] < 1 else None, axis=1
                                    )
                                    updated_df.loc[selected_mask, 'Days_1_to_30'] = updated_df.loc[selected_mask].apply(
                                        lambda row: row['Balance Due'] if row['AgingDays'] is not None and 1 <= row['AgingDays'] <= 30 else None, axis=1
                                    )
                                    updated_df.loc[selected_mask, 'Days_31_to_60'] = updated_df.loc[selected_mask].apply(
                                        lambda row: row['Balance Due'] if row['AgingDays'] is not None and 31 <= row['AgingDays'] <= 60 else None, axis=1
                                    )
                                    updated_df.loc[selected_mask, 'Days_61_to_90'] = updated_df.loc[selected_mask].apply(
                                        lambda row: row['Balance Due'] if row['AgingDays'] is not None and 61 <= row['AgingDays'] <= 90 else None, axis=1
                                    )
                                    updated_df.loc[selected_mask, 'Over_91_Days'] = updated_df.loc[selected_mask].apply(
                                        lambda row: row['Balance Due'] if row['AgingDays'] is not None and row['AgingDays'] >= 91 else None, axis=1
                                    )
                                    updated_df.loc[selected_mask, 'Total Target'] = updated_df.loc[selected_mask].apply(
                                        lambda row: row['Balance Due'] if row['AgingDays'] is not None and row['AgingDays'] > 0 else 0, axis=1
                                    )
                            
                            # Reattach grand total if it existed
                            if not grand_total.empty:
                                display_df6_view = pd.concat([updated_df, grand_total], ignore_index=True)
                            else:
                                display_df6_view = updated_df
                            
                            # Add to display_df_name for tracking (by Name only, no dept)
                            if 'display_df_name' not in st.session_state:
                                st.session_state.display_df_name = pd.DataFrame(columns=['Name', 'dept_code', 'Days'])
                            if 'dept_code' not in st.session_state.display_df_name.columns:
                                st.session_state.display_df_name['dept_code'] = ''
                            if 'Days' not in st.session_state.display_df_name.columns:
                                st.session_state.display_df_name['Days'] = 30
                            
                            auto_dn = st.session_state.display_df_name
                            for auto_name in auto_add_names:
                                already = auto_dn['Name'].astype(str).str.strip().str.casefold() == auto_name.strip().casefold()
                                if not already.any():
                                    st.session_state.display_df_name = pd.concat(
                                        [st.session_state.display_df_name, pd.DataFrame({'Name': [auto_name], 'dept_code': [''], 'Days': [30]})],
                                        ignore_index=True
                                    )
                                    auto_dn = st.session_state.display_df_name
                                else:
                                    st.session_state.display_df_name.loc[already, 'Days'] = 30
                        
                        # Apply category conditions to add DSS2_Name, Category columns and update SR2
                        display_df6_view = apply_category_to_display_df(display_df6_view)
                        
                        # Apply re-tag history on load: updates SR2, SR_Code2, DSS_NAME and DSS (except Original Data download)
                        display_df6_view = apply_re_tag_history_to_df(display_df6_view)
                        display_df6_view = apply_re_tag_history_dss_to_df(display_df6_view)
                        
                        # Store the initialized display_df6_view in session state
                        st.session_state.display_df6_view = display_df6_view
                    else:
                        # If not initializing, check if category columns exist, if not apply the logic
                        if 'DSS2_Name' not in st.session_state.display_df6_view.columns or 'Category' not in st.session_state.display_df6_view.columns:
                            st.session_state.display_df6_view = apply_category_to_display_df(st.session_state.display_df6_view)
                        else:
                            # Reapply category logic to ensure SR2 is updated based on current Balance Due values
                            st.session_state.display_df6_view = apply_category_to_display_df(st.session_state.display_df6_view)
                        # Apply re-tag history: updates SR2, SR_Code2, DSS_NAME and DSS for all displays and state
                        st.session_state.display_df6_view = apply_re_tag_history_to_df(st.session_state.display_df6_view)
                        st.session_state.display_df6_view = apply_re_tag_history_dss_to_df(st.session_state.display_df6_view)
                        # Sync display_df6s (Collection Performance tab) with same SR2, SR_Code2, DSS_NAME and DSS updates
                        if 'display_df6s' in st.session_state and not st.session_state.display_df6s.empty:
                            st.session_state.display_df6s = apply_re_tag_history_to_df(st.session_state.display_df6s.copy())
                            st.session_state.display_df6s = apply_re_tag_history_dss_to_df(st.session_state.display_df6s.copy())
                    
                    # Call fragment - it handles both controls AND dataframe display
                    CR_btn_1_fragment()
                    
            with stabz2:
                asof_month = (datetime.strptime(date_to_str, '%Y-%m-%d') + relativedelta(months=1,day=31)).strftime('%m-%Y').upper()
                stabz2_tab1, stabz2_tab2 = st.tabs(["Summarize Collection", "Row-Detailed Collection"])
                
                with stabz2_tab1:
                    with st.spinner("Collection Report for the month - Processing..."):
                        st.subheader(f"Collection Report for the month of [{asof_month}] - (base on AR as-of)")      
                        result_df7 = st.session_state.result_df7.copy()
                        
                        display_df7 = result_df7.copy()                            
                        display_df7['CollectedAmount'] = pd.to_numeric(display_df7['CollectedAmount'], errors='coerce')
                        display_df7.rename(columns={
                                    'CollectedAmount': 'Collected_Amount',
                                    'PaidUnpaid': 'Remaining Balance'}, inplace=True)
                        
                        col_cfg_df7 = _numeric_column_config(display_df7)
                        st.dataframe(display_df7, use_container_width=True, hide_index=True, column_config=col_cfg_df7)
                        
                        # Button in fragment
                        CR_btn_2_fragment()
                
                with stabz2_tab2:
                    with st.spinner("Row-Detailed Collection - Processing..."):
                        st.subheader(f"Row-Detailed Collection for the month of [{asof_month}] - (sp_AR_CombinedCollectionReport)")      
                        CR_btn_2_cc_fragment()
        with tabz1:
            # MAIN DATAFRAME
            st.subheader(f"Direct Sales Related Collection Report [ {date_from_str} to {date_to_str} ]")
            display_df = result_df.copy()

            #######################################################################################
            col_cfg_disp = _numeric_column_config(display_df)
            st.dataframe(display_df, use_container_width=True, hide_index=True, column_config=col_cfg_disp)
           
            # Button in fragment
            DS_btn_1_fragment()           
        ###################################
        
        st.subheader(f"Financial Overview {date_from_str} to {date_to_str}")
        
        total_remaining_balance = result_df[result_df['remaining_balance'] > 0]['remaining_balance'].sum()
        total_paid_amount = result_df[result_df['remaining_balance'] <= 0]['gross_ar'].sum()
        count_total_paid = result_df[result_df['remaining_balance'] <= 0].shape[0]
        count_total_unpaid = result_df[result_df['remaining_balance'] > 0].shape[0]
        
        current_date = datetime.now()
        if 'inv_dr_date' in result_df.columns:
            result_df['inv_dr_date'] = pd.to_datetime(result_df['inv_dr_date'], errors='coerce')
            result_df['payment_terms'] = result_df['payment_terms'].astype(str).str.extract(r'(\d+)')[0].astype(float).fillna(0)
            result_df['days_overdue'] = result_df.apply(
                lambda row: (current_date - row['inv_dr_date']).days - row['payment_terms'] 
                if pd.notnull(row['inv_dr_date']) and pd.notnull(row['payment_terms']) and row['remaining_balance'] > 0 else 0,
                axis=1
            )
            overdue_df = result_df[(result_df['days_overdue'] > 0) & (result_df['remaining_balance'] > 0)].copy()
            overdue_count = overdue_df.shape[0]
            total_overdue_amount = overdue_df['remaining_balance'].sum()
        else:
            overdue_count = 0
            total_overdue_amount = 0
            
        unpaid_df = result_df[result_df['remaining_balance'] > 0]
        if not unpaid_df.empty:
            max_unpaid_row = unpaid_df.loc[unpaid_df['remaining_balance'].idxmax()]
            max_amount_unpaid = max_unpaid_row['remaining_balance']
            customer_name = max_unpaid_row.get('customer_name', 'N/A')
            max_unpaid_display = f"{customer_name}<br>PHP {max_amount_unpaid:,.2f}"
        else:
            max_unpaid_display = "N/A"

        if not overdue_df.empty:
            max_overdue_row = overdue_df.loc[overdue_df['remaining_balance'].idxmax()]
            max_overdue_amount = max_overdue_row['remaining_balance']
            overdue_customer_name = max_overdue_row.get('customer_name', 'N/A')
            max_overdue_display = f"{overdue_customer_name}<br>PHP {max_overdue_amount:,.2f}"
        else:
            max_overdue_display = "N/A"

        tile_style = """<div style="background: linear-gradient(135deg, #7C4DFF 0%, #5E35B1 100%); padding: 4px; text-align: center; 
                color: #F5F6FE; border-radius: 14px; width: 100%; height: 100px; display: flex; flex-direction: column; 
                align-items: center; font-family: 'Inter', 'Roboto', sans-serif; font-size: clamp(10px, 2.3vw, 14px); 
                margin: 8px; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15); transition: transform 0.3s ease, box-shadow 0.3s ease;"
                onmouseover="this.style.transform='scale(1.03)'; this.style.boxShadow='0 6px 16px rgba(0, 0, 0, 0.25)'" 
                onmouseout="this.style.transform='scale(1)'; this.style.boxShadow='0 4px 12px rgba(0, 0, 0, 0.15)'">
                <div style="background-color: #5b2f90; padding: 1px; border-top-left-radius: 10px; border-top-right-radius: 10px; width: 100%;
                border-top: 2px solid #B388FF; border-bottom: 2px solid #B388FF; border-top-left-radius: 14px; border-top-right-radius: 14px;">
                    <h3 style="margin: 0; font-size: clamp(8px, 2vw, 12px);">{title}</h3>
                </div>
                        <h1 style="margin: 0; font-size: clamp(10px, 2.5vw, 14px); padding-top: 5px;">{value}</h1>
                    </div>"""

        cols = st.columns([2, 1, 1, 1])
        with cols[0]:
            st.markdown(tile_style.format(title="Max Amount With Remaining Balance", value=max_unpaid_display), unsafe_allow_html=True)
        with cols[1]:
            st.markdown(tile_style.format(title="Total Paid Amount", value=f"PHP {total_paid_amount:,.2f}"), unsafe_allow_html=True)
        with cols[2]:
            st.markdown(tile_style.format(title="Total Remaining Balance", value=f"PHP {total_remaining_balance:,.2f}"), unsafe_allow_html=True)
        with cols[3]:
            st.markdown(tile_style.format(title="Total Overdue Amount", value=f"PHP {total_overdue_amount:,.2f}"), unsafe_allow_html=True)

        with cols[0]:
            st.markdown(tile_style.format(title="Max Overdue With Remaining Balance", value=max_overdue_display), unsafe_allow_html=True)
        with cols[1]:
            st.markdown(tile_style.format(title="Count of Total Paid", value=count_total_paid), unsafe_allow_html=True)
        with cols[2]:
            st.markdown(tile_style.format(title="Count of Total Unpaid", value=count_total_unpaid), unsafe_allow_html=True)
        with cols[3]:
            st.markdown(tile_style.format(title="Count of Total Overdue", value=overdue_count), unsafe_allow_html=True)

        st.title(" ")
        ###################################
        
        
        title2, title1 = st.tabs(["COLLECTION RELATED DATA","SALES RELATED DATA"])
        with title1:
            with st.spinner("SALES RELATED DATA is Processing..."):
                st.subheader(f"DSM RELATED DATA (Sales Incentive) - [ {date_from_str} to {date_to_str} ]")        
                tab1, tab2, tab3, tab4 = st.tabs(["DSM Collection Summary Report", "Paid/Unpaid Incentives", "Overdue Summary", "Rebates Summary"])
                st_result_df = st.session_state.result_df
                st_result_df3 = st.session_state.result_df3  # noqa: F841
                st_result_df4 = st.session_state.result_df4  # noqa: F841
                date_from_str = st.session_state.date_from_str
                date_to_str = st.session_state.date_to_str 
                with tab1:            
                    st.markdown("#### Filter by DSM with Remaining Amount")
                    
                    dsm_summary = st_result_df.groupby(['dsm', 'dept', 'dept_code']).agg({'remaining_balance': 'sum'}).reset_index()
                    dsm_summary = dsm_summary[dsm_summary['remaining_balance'] > 0].groupby(['dsm', 'dept_code']).agg({
                        'remaining_balance': 'sum',
                        'dept': 'first'
                    }).reset_index()
                    
                    dsm_summary = dsm_summary.sort_values(by=['dsm','dept_code'])
                    
                    dsm_options = ['All'] + [f"{row['dsm']} - (Remaining Balance: PHP {row['remaining_balance']:,.2f}, Dept: {row['dept_code']})" 
                                        for _, row in dsm_summary.iterrows()]
                    
                    selected_dsm = st.selectbox("Select DSM", dsm_options, key="dsm_selectbox")
                    st.session_state.selected_dsm = selected_dsm

                    selected_dsm_value = selected_dsm if selected_dsm == 'All' else selected_dsm.split(' - (')[0]
                    selected_dept_value = None if selected_dsm == 'All' else selected_dsm.split('Dept: ')[1].rstrip(')')
                    
                    if selected_dsm_value == 'All':
                        filtered_df = st_result_df[st_result_df['remaining_balance'].notna() & (st_result_df['remaining_balance'] != 0)]
                    else:
                        filtered_df = st_result_df[(st_result_df['dsm'] == selected_dsm_value) & 
                                            (st_result_df['dept_code'].astype(str) == selected_dept_value) &
                                            st_result_df['remaining_balance'].notna() & 
                                            (st_result_df['remaining_balance'] != 0)]
                        
                    base_columns = ['pmr', 'customer_name', 'dept_code']
                    display_columns = base_columns + ['remaining_balance']
                    dynamic_columns = [col for col in st_result_df.columns[st_result_df.columns.get_loc('detaildate') + 1:]
                                    if re.match(r'^\d{2}-[a-z]{3}$', col) or col.startswith('%_')]
                    display_columns.extend(dynamic_columns)

                    display_columns = [col for col in display_columns if col in filtered_df.columns]

                    group_columns = ['pmr', 'customer_name', 'dept_code']
                    agg_dict = {'remaining_balance': 'sum'}
                    for col in dynamic_columns:
                        if col in filtered_df.columns:
                            agg_dict[col] = 'sum'
                    
                    filtered_df = filtered_df.groupby(group_columns).agg(agg_dict).reset_index()
                    filtered_df = filtered_df[display_columns]
                    filtered_df = filtered_df.sort_values(by=['dept_code','pmr', 'customer_name'])

                    if not filtered_df.empty:
                        filtered_display_df = filtered_df.copy()
                        numeric_cols = filtered_display_df.select_dtypes(include=['float64', 'int64']).columns
                        for col in numeric_cols:
                            filtered_display_df[col] = filtered_display_df[col].apply(
                                lambda x: f"{x:,.2f}" if pd.notna(x) else None
                            )

                        col_cfg_filt = _numeric_column_config(filtered_display_df)
                        st.dataframe(filtered_display_df, use_container_width=True, hide_index=True, column_config=col_cfg_filt)        
                        
                        filtered_csv = filtered_df.to_csv(index=False)
                        st.download_button(
                            label="Download DSM Collection Summary Report",
                            data=filtered_csv,
                            file_name=f"DSM_{selected_dsm_value}_Summary_Report_with_PMR_{date_from_str} - {date_to_str}.csv",
                            mime="text/csv"
                        )
                    else:
                        st.info("No data available for the selected DSM with non-zero remaining balance.")

                with tab2:
                    st.markdown("#### PMR Paid/Unpaid Incentives")
                    # st.info("Paid/Unpaid Incentives reports functionality to be implemented.")
                                    
                    # # Group by pmr and dept_code, and sort by dept_code then pmr
                    # pmr_summary = st_result_df.groupby(['pmr', 'dept_code'])
                    # sorted_keys = sorted(pmr_summary.groups.keys(), key=lambda x: (x[1], x[0]))

                    # # Create a mapping of display labels to pmr values
                    # cr_options = ['All'] + [f"{pmr} - Dept: {dept_code}" for pmr, dept_code in sorted_keys]
                    # cr_mapping = {'All': 'All'}  # Map 'All' to itself
                    # for pmr, dept_code in sorted_keys:
                    #     display_label = f"{pmr} - Dept: {dept_code}"
                    #     cr_mapping[display_label] = pmr  # Map display label to the raw pmr value

                    # # Use the display labels in the selectbox
                    # selected_cr_label = st.selectbox("Select Professional Medical Representative (PMR)", cr_options, key="pmr_selectbox")

                    # # Get the actual pmr value from the mapping
                    # selected_cr = cr_mapping[selected_cr_label]

                    # # Filter DataFrame based on PMR selection
                    # if selected_cr == 'All':
                    #     filtered_df = st_result_df
                    # else:
                    #     filtered_df = st_result_df[st_result_df['pmr'] == selected_cr]

                    # # Identify dynamic columns starting with '%_' after detaildate
                    # dynamic_columns = [col for col in st_result_df.columns[st_result_df.columns.get_loc('detaildate') + 1:] 
                    #                         if col.startswith('%_')]

                    # # Prepare the DataFrame with month_year
                    # filtered_df['inv_dr_date'] = pd.to_datetime(filtered_df['inv_dr_date'])
                    # filtered_df['month_year'] = filtered_df['inv_dr_date'].dt.strftime('%b-%Y').str.upper()
                    # # Add a sortable month_year column for proper sorting (YYYY-MM format)
                    # filtered_df['month_year_sort'] = filtered_df['inv_dr_date'].dt.strftime('%Y-%m')

                    # # Group by pmr and month_year
                    # grouped = filtered_df.groupby(['pmr', 'month_year', 'month_year_sort'])

                    # # Calculate metrics per group
                    # performance_data = []
                    # for (pmr, month, sort_key), group in grouped:
                    #     # Sum of negative values in dynamic columns (total collected)
                    #     # total_collected = sum(group[col][group[col] < 0].sum() for col in dynamic_columns if col in group.columns)
                        
                    #     collection_count = sum((group[col] < 0).sum() for col in dynamic_columns if col in group.columns)
                        
                    #     # Total initial amount (sum of gross_ar)
                    #     total_initial = group['gross_ar'].sum() if 'gross_ar' in group.columns else 0
                    #     total_remaining = group['remaining_balance'].sum() if 'remaining_balance' in group.columns else 0
                    #     total_collected = total_initial - total_remaining
                    #     # Collection rate (avoid division by zero)
                    #     collection_rate = (abs(total_collected) / total_initial) if total_initial > 0 else 0
                        
                    #     # Average collection amount
                    #     avg_collection = abs(total_collected) / collection_count if collection_count > 0 else 0  # noqa: F841
                        
                    #     # Department (assumed consistent per pmr)
                    #     dept = group['dept_code'].dropna().iloc[0] if 'dept_code' in group.columns and not group['dept_code'].isna().all() else None
                        
                    #     performance_data.append({
                    #         'PMR': pmr,
                    #         'Total Gross AR (PHP)': total_initial,
                    #         'Total Collected Amount (PHP)': abs(total_collected),
                    #         'Collection Percent Rate (%)': collection_rate * 100,  # Convert to percentage (0.89 -> 89)
                    #         'Department': dept,
                    #         'Month_Year': month,
                    #         'month_year_sort': sort_key  # Temporary column for sorting
                    #     })

                    # # Create performance DataFrame
                    # performance_df = pd.DataFrame(performance_data)

                    # # Sort by PMR and month_year_sort for proper chronological order
                    # performance_df = performance_df.sort_values(by=['PMR', 'month_year_sort'])
                    
                    # # Format Collection Percent Rate (%) as a string with % symbol
                    # performance_df['Collection Percent Rate (%)'] = performance_df['Collection Percent Rate (%)'].apply(lambda x: f"{x:.0f}%")
                                    
                    # # Ensure st_result_df3 has the required columns before selecting
                    # if 'pmr' in st_result_df3.columns and 'inv_month' in st_result_df3.columns:
                    #     st_result_df3 = st_result_df3[['no_','pmr','%_perf', 'inv_month', 'total_incentive', 'remaining_incentive']]
                    #     # Merge on both pmr and month_year (inv_month)
                    #     performance_df = performance_df.merge(st_result_df3, left_on=['PMR', 'Month_Year'], right_on=['pmr', 'inv_month'], how='left')
                        
                    #     performance_df['%_perf'] = performance_df['%_perf'] * 100
                        
                    #     # Format Collection Percent Rate (%) as a string with % symbol
                    #     performance_df['%_perf'] = performance_df['%_perf'].apply(lambda x: f"{x:.0f}%") 
                    #     performance_df['%_perf'] = performance_df['%_perf'].str.replace('nan','')            
                    #     performance_df.rename(columns={
                    #         'inv_month': 'MONTH PERFORM',
                    #         'total_incentive': 'TOTAL INCENTIVE',
                    #         'remaining_incentive': 'REMAINING/UNPAID INCENTIVE',
                    #         '%_perf' : 'ABOVE 80% PERF',
                    #         'month_year_sort' : 'COLLECTION MONTH'
                    #     }, inplace=True)                
                    #     performance_df = performance_df.drop(columns=['pmr','Month_Year'], errors='ignore')
                    # else:
                    #     st.write("Warning: 'pmr' or 'inv_month' column not found in st_result_df3. Merge skipped.")
                    
                    # # Sort by total collected amount (descending)
                    # performance_df = performance_df.sort_values(by=['Department','PMR'], ascending=[True,True])
                    
                    # # Format numeric columns for display
                    # performance_display_df = performance_df.copy()
                    # numeric_cols = ['Total Collected Amount (PHP)', 'Total Gross AR (PHP)', 
                    #                 'TOTAL INCENTIVE','REMAINING/UNPAID INCENTIVE']
                    # for col in numeric_cols:
                    #     performance_display_df[col] = performance_display_df[col].apply(
                    #         lambda x: f"{x:,.2f}" if pd.notna(x) else "0.00"
                    #     )
                    # performance_display_df['no_'] = performance_display_df['no_'].apply(
                    #     lambda x: int(x) if pd.notna(x) else 0
                    # )
                    # # MAKE THIS CLICKABLE THEN LOAD MORE INFORMATION BELOW AS THE PAYROLL PAYMENT base on condition no_ = entry_no
                    # #####################################################################################
                    # # Display the performance table
                    # if not performance_df.empty:
                    #     st_numeric_cols = performance_display_df.select_dtypes(include=['float64']).columns
                    #     # Format all numeric values with comma separators and 2 decimal places
                    #     for col in st_numeric_cols:
                    #         performance_display_df[col] = performance_display_df[col].apply(
                    #             lambda x: f"{x:,.2f}" if pd.notna(x) else None
                    #         )
                    #     performance_display_df = performance_display_df.fillna("")
                    #     event = st.dataframe(performance_display_df, use_container_width=True, hide_index=True, on_select="rerun", selection_mode="single-row")
                        
                    #     # CSV download button
                    #     performance_csv = performance_df.to_csv(index=False)
                    #     st.download_button(
                    #         label="Download PMR Incentive Monitoring Report",
                    #         data=performance_csv,
                    #         file_name=f"Incentive_Monitoring_Report_{selected_cr}_{date_from_str}_to_{date_to_str}.csv",
                    #         mime="text/csv"
                    #     )
                        
                    #     # Check if a row is selected
                    #     if event.selection.rows:
                    #         selected_row_index = event.selection.rows[0]
                    #         selected_no = performance_display_df.iloc[selected_row_index]['no_']
                    #         selected_pmr = performance_display_df.iloc[selected_row_index]['PMR'] 
                    #         selected_mnt = performance_display_df.iloc[selected_row_index]['COLLECTION MONTH'] 
                    #         selected_tintv = float(performance_display_df.iloc[selected_row_index]['TOTAL INCENTIVE'].replace(",",""))
                    #         selected_reui = float(performance_display_df.iloc[selected_row_index]['REMAINING/UNPAID INCENTIVE'].replace(",","")) 
                    #         # selected_tgAR = float(performance_display_df.iloc[selected_row_index]['Total Gross AR (PHP)'].replace(",",""))                    
                            
                    #         selected_tpaid = selected_tintv-selected_reui
                            
                    #         # Filter the second dataframe based on the selection
                    #         additional_data = st_result_df4[st_result_df4['entry_no'] == selected_no]
                    #         # Select Few Columns Only additional_data
                    #         additional_data = additional_data[[ 
                    #             'year_pay', 'years',
                    #             'jan_15', 'jan_30', 'feb_15', 'feb_28', 'mar_15', 'mar_30', 'apr_15', 'apr_30',                         
                    #             'may_15', 'may_30', 'jun_15', 'jun_30', 'jul_15', 'jul_30', 'aug_15', 'aug_30',                         
                    #             'sep_15', 'sep_30', 'oct_15', 'oct_30', 'nov_15', 'nov_30', 'dec_15', 'dec_30',                         
                    #             'last_updated'
                    #         ]]
                    #         additional_data.rename(columns={
                    #             'year_pay': 'PAYROLL YEAR',
                    #             'years': 'YEAR OF MONTHS'
                    #         }, inplace=True) 
                            
                    #         # Display the additional data
                    #         if not additional_data.empty:
                    #             st.subheader(f"Details of Paid Payroll Incentives: {selected_pmr} | Total Paid Incentive: {selected_tpaid:,.2f}")
                    #             st_numeric_cols = additional_data.select_dtypes(include=['float64']).columns
                    #             # Format all numeric values with comma separators and 2 decimal places
                    #             for col in st_numeric_cols:
                    #                 additional_data[col] = additional_data[col].apply(
                    #                     lambda x: f"{x:,.2f}" if pd.notna(x) else None
                    #                 )                        
                    #             additional_data = additional_data.fillna("")
                    #             st.dataframe(additional_data, use_container_width=True, hide_index=True)
                    #         else:
                    #             st.warning(f"No additional data found for Entry No: {selected_no}")
                            
                    #         # grouped = filtered_df.groupby(['pmr', 'month_year', 'month_year_sort'])    
                    #         # 'COLLECTION MONTH'
                    #         st_result_df['month_year_sort'] = st_result_df['inv_dr_date'].dt.strftime('%Y-%m')
                    #         additional_data2 = st_result_df[(st_result_df['pmr'] == selected_pmr) & (st_result_df['month_year_sort'] == selected_mnt) ]
                    #         base_columns = ['customer_name','pm', 'inv_dr_no','detailamount', 'remaining_balance','%_share_ar'] 
                    #         display_columns = base_columns
                    #         dynamic_columns = [col for col in result_df.columns[result_df.columns.get_loc('detaildate') + 1:] if col.startswith('%_')]
                    #         display_columns.extend(dynamic_columns)  
                    #         additional_data2 = additional_data2[display_columns]
                    #         total_remaning = additional_data2['remaining_balance'].sum()
                    #         additional_data2.rename(columns={
                    #             'remaining_balance': 'REMAINING AMOUNT',
                    #             'detailamount': 'DETAILED COLLECTION AMOUNT'
                    #         }, inplace=True)                                                         
                    #         # Display the additional data
                    #         if not additional_data2.empty:
                    #             st.subheader(f"Details of Collections with Total Remaining Amount of {total_remaning:,.2f}")
                    #             st_numeric_cols = additional_data2.select_dtypes(include=['float64']).columns
                    #             # Format all numeric values with comma separators and 2 decimal places
                    #             for col in st_numeric_cols:
                    #                 additional_data2[col] = additional_data2[col].apply(
                    #                     lambda x: f"{x:,.2f}" if pd.notna(x) else None
                    #                 )      
                    #             additional_data2 = additional_data2.fillna("")                     
                    #             st.dataframe(additional_data2, use_container_width=True, hide_index=True, key = "additional_data2")
                    #         else:
                    #             st.warning(f"No additional data found for Entry No: {selected_no}")                    
                    #####################################################################################
                        
                with tab3:
                    print()
                    # Overdue_fragment()                        
                        
                with tab4:
                    with st.spinner("Processing Rebates Procedures..."):
                        st.markdown("#### Rebate Summary")
                        # result_df = st.session_state.result_df
                        # result_df5 = st.session_state.result_df5
                        # date_from_str = st.session_state.date_from_str
                        # date_to_str = st.session_state.date_to_str
                        # required_display_columns = ['customer_name','inv_dr_date','inv_dr_no','no_', 'remaining_balance', 'gross_ar', 'net_sales_less_rud_vat_disc','detailamount','total_masterlist_pcnt']           
                        # # filtered_df = result_df[result_df['remaining_balance'] != 0][required_display_columns]            
                        # # filtered_df['Collected % (Net)'] = (filtered_df['net_sales_less_rud_vat_disc'] - filtered_df['remaining_balance']) / filtered_df['net_sales_less_rud_vat_disc'] * 100
                        # # filtered_df['Collected % (Gross)'] = (filtered_df['gross_ar'] - filtered_df['remaining_balance']) / filtered_df['gross_ar'] * 100
                        
                        # filtered_df = pd.merge(filtered_df, result_df5, left_on='no_', right_on='No_', how='inner')
                        # filtered_df = filtered_df[filtered_df['remaining_balance'] != 0][required_display_columns]
                        # filtered_df = filtered_df.drop_duplicates()
                        
                        # filtered_df['total_masterlist_pcnt'] = filtered_df['total_masterlist_pcnt'] * 100
                        # filtered_df['total_rebate_pcnt'] = filtered_df['total_masterlist_pcnt']    
                        
                        # filtered_df = filtered_df[filtered_df['total_rebate_pcnt'] != 0]
                        
                        # filtered_df['Collected % (Net)'] = ((filtered_df['net_sales_less_rud_vat_disc'] - filtered_df['remaining_balance']) / filtered_df['net_sales_less_rud_vat_disc'] * 100).clip(lower=0)
                        # filtered_df['Collected % (Gross)'] = ((filtered_df['gross_ar'] - filtered_df['remaining_balance']) / filtered_df['gross_ar'] * 100).clip(lower=0)
                        # filtered_df['highlight_flag'] = ((filtered_df['Collected % (Net)'] < filtered_df['total_rebate_pcnt']) | (filtered_df['Collected % (Gross)'] < filtered_df['total_rebate_pcnt'])) & (filtered_df['Collected % (Net)'] > 1) & (filtered_df['Collected % (Gross)'] > 1)
                        
                        # # Sort by remaining_balance
                        # filtered_df = filtered_df.sort_values(by='highlight_flag', ascending=False)
                                    
                        # def highlight_rows(s):
                        #     return ['background-color: #f28b82' if s['highlight_flag'] else '' for _ in s.index]

                        # # Apply styling
                        # styled_df = filtered_df.style.apply(highlight_rows, axis=1)

                        # # Display the styled DataFrame in Streamlit
                        # st.dataframe(styled_df, use_container_width=True, hide_index=True, key='rebate_remarks')          
                                        
#############################################################################################################################################################################################################################################################################################################################################################################################                                
        with title2:
            with st.spinner("COLLECTION PERFORMANCE RELATED DATA is Processing..."):
                asof_month = (datetime.strptime(date_to_str, '%Y-%m-%d') + relativedelta(months=1,day=31)).strftime('%m-%Y').upper()
                # st.subheader(f"COLLECTION PERFORMANCE DATA (BU7) - [ Collection Month of {asof_month} ]")
                # Create tabs for DSS
                tabs3, tabs2, tabs4 = st.tabs(["Collection Performance Target with Category", "Collection Performance - Related Data", "Other Related Data"])                        
##____________________________________________________________________________________________________________________________________________________________________________________________________________________________________#
                with tabs2:
                    sstabz1, sstabz2, sstabz3, sstabz4, sstabz5, sstabz6, sstabz7  = st.tabs(["A/R for Collection Performance", "Target COD", "Target CURRENT","Target OVERDUE","Returns for Target",  "Other Adjustments for Target", "Target Base Collection Performance Summary"])
                    with sstabz1:
                        with st.spinner("A/R for Collection Performance - Processing..."):                                                    
                            # Auto - Update after edit [add days]
                            # option1 : delete all values in Current, 1 to 30, 31 to 60, 61 to 90, 91+ days
                            # then [add days] in [Due Date] column
                            # then re-calculate the formula for Current, 1 to 30, 31 to 60, 61 to 90, 91+ days base on [due date]
                            date_to_str = st.session_state.date_to_str
                            
                            if 'display_df6s' not in st.session_state:
                                if 'display_df6_view' not in st.session_state or st.session_state.display_df6_view.empty:
                                    st.error("Error: display_df6_view not found in session state. Please ensure data is loaded.")
                                else:
                                    # Perform left join with df8 to update ADD Days
                                    if 'result_df8' not in st.session_state:
                                        st.error("Error: result_df8 not found in session state. Please ensure result_df8 is loaded.")
                                    else:
                                        
                                        result_df6a = st.session_state.display_df6_view.copy()
                                        result_df6a['ADD Days'] = 0
                                        # result_df6a = st.session_state.result_df6a.copy()
                                        
                                        # result_df6a.to_csv("debug_result_df6a_initial.csv", index=False)  # Debugging line to check the initial content of result_df6a
                                        df8 = st.session_state.result_df8.copy()
                                        df8a = st.session_state.result_df8a.copy()
                                        # Ensure ADD Days column exists, but don’t pre-fill with 0
                                        if 'ADD Days' not in result_df6a.columns:
                                            result_df6a['ADD Days'] = pd.NA  # Use NA to avoid defaulting to 0
                                            
                                        # result_df6a.to_csv("debug_result_df6a_before_merge.csv", index=False)  # Debugging line to check the content of result_df6a
                                                                                
                                        # Perform left join to match Code with Dimension Value Code
                                        # result_df6a.to_csv("debug_AR_result_df6a_before_merge.csv", index=False)  # Debugging line to check the content of result_df6a
                                        
                                        # First branch: Forcefully replace all ADD Days with ADD_DAYS, filling NaN with 0, only for 'INVOICE'
                                        df8 = df8[['DSS_CODE', 'ADD_DAYS']].copy().drop_duplicates()
                                        merged_df = pd.merge(result_df6a, df8[['DSS_CODE', 'ADD_DAYS']], 
                                                            left_on='DSS', 
                                                            right_on='DSS_CODE', 
                                                            how='left')
                                        bal_col_sql = next((c for c in ['Balance Due', 'Remaining Balance', 'BalanceDue'] if c in merged_df.columns), None)
                                        balance_positive_sql = pd.to_numeric(merged_df[bal_col_sql], errors='coerce').fillna(0) >= 0 if bal_col_sql else pd.Series([True] * len(merged_df), index=merged_df.index)
                                        merged_df['ADD Days'] = np.where(
                                            ((merged_df['DOCUMENT TYPE'] == 'INVOICE') | 
                                            (merged_df['DOCUMENT TYPE'].fillna('').str.strip() == '')) & 
                                            ~(merged_df['Payment_Terms'].fillna('').str.contains('CONTRACT', case=False, na=False)) & balance_positive_sql,
                                            pd.to_numeric(merged_df['ADD_DAYS'], errors='coerce').fillna(0).astype(float),
                                            merged_df['ADD Days']  # Preserve existing 'ADD Days' for non-INVOICE rows
                                        )
                                        merged_df = merged_df.drop(columns=['ADD_DAYS'], errors='ignore')
                                        
                                        # merged_df.to_csv("debug_AR_merged_df_after_first_branch_merge.csv", index=False)  # Debugging line to check the content of merged_df
                                        
                                        # THERE IS A POSSIBILITY THAT THIS SECOND BRANCH SWITCH TO FIRST BRANCH
                                        ##########################################################################################
                                        # Second branch: Replace only 0 in ADD Days with ADD_DAYS, preserving NaN and non-zero values, only for 'INVOICE'
                                        if sproc8a == "sp_AR_AddDays":
                                            df8a = df8a[['CUSTOMER_NO', 'ADD_DAYS']].copy().drop_duplicates()
                                            merged_df = pd.merge(merged_df, df8a[['CUSTOMER_NO', 'ADD_DAYS']], 
                                                                left_on='Customer No_', 
                                                                right_on='CUSTOMER_NO', 
                                                                how='left')
                                            # Keep 888xx as string so prefix (age-bucket) logic can match; use numeric for others (same as modal)
                                            # Do not apply add_days to rows with negative balance
                                            add_days_raw = merged_df['ADD_DAYS'].astype(str)
                                            is_888 = add_days_raw.str.startswith('888', na=False)
                                            inv = (merged_df['DOCUMENT TYPE'] == 'INVOICE')
                                            merged_df['ADD Days'] = np.where(
                                                ~inv,
                                                merged_df['ADD Days'],
                                                np.where(~balance_positive_sql, 0,
                                                    np.where(is_888, merged_df['ADD_DAYS'], pd.to_numeric(merged_df['ADD_DAYS'], errors='coerce').fillna(0).astype(float)))
                                            )
                                            merged_df = merged_df.drop(columns=['ADD_DAYS'], errors='ignore')   
                                                                                     
                                        # merged_df.to_csv("debug_merged_df_after_merge.csv", index=False)  # Debugging line to check the content of merged_df
                                        # Update ADD Days with Add where available, default to 0 for non-matches
                                        # merged_df['ADD Days'] = merged_df['ADD_DAYS'].fillna(0).astype(float)
                                        # Drop the temporary Add and Code columns
                                        # merged_df = merged_df.drop(columns=['Add', 'Code'], errors='ignore')
                                        merged_df = merged_df.drop_duplicates()
                                        
                                        # merged_df.to_csv("debug_AR_merged_df_after_add_days_merge.csv", index=False)  # Debugging line to check the content of merged_df
                                        
                                        # Condition to reset ADD Days to 0 if Payment Terms > 45 and ADD Days > 0 (numeric only; leave 888xx untouched)
                                        # Exclude HOSP000058, HOSP000526 - they keep ADD Days in data_editor (exemption only in modal display_merged)
                                        merged_df['Payment_Terms_Numeric'] = merged_df['Payment_Terms'].str.extract(r'(\d+)').astype(float).fillna(0).astype(int)
                                        add_days_numeric = pd.to_numeric(merged_df['ADD Days'], errors='coerce').fillna(0)
                                        mask = (merged_df['Payment_Terms_Numeric'] > 45) & (add_days_numeric > 0)
                                        cust_no_col = 'Customer No_' if 'Customer No_' in merged_df.columns else None
                                        if cust_no_col:
                                            mask = mask & (~merged_df['Customer No_'].astype(str).str.strip().isin(['HOSP000058', 'HOSP000526']))
                                        remark_text = "Due Date not adjusted, Payment Terms is >= 60 days"
                                        merged_df.loc[mask, 'Remarks'] = merged_df.loc[mask, 'Remarks'].fillna('') + " | " + remark_text  
                                        merged_df.loc[mask, 'ADD Days'] = 0  # Reset ADD Days to 0 if not adjusted  
                                        
                                        # Do not apply add_days to rows with negative Balance Due / Remaining Balance / BalanceDue
                                        if bal_col_sql:
                                            merged_df.loc[~balance_positive_sql, 'ADD Days'] = 0
                                        
                                        merged_df = merged_df.drop(columns=['Payment_Terms_Numeric'], errors='ignore')
                                        
                                        # merged_df.to_csv("debug_AR_merged_df_after_add_days_update.csv", index=False)  # Debugging line to check the content of merged_df    
                                        
                                        merged_df['AsOfDate'] = pd.to_datetime(merged_df['AsOfDate'], errors='coerce')
                                        # Create a temporary string version for the mask without modifying the original column
                                        configs = [
                                            {'prefix': '88801', 'target_days': 1, 'remark': 'Maintain to 1 - 30 days'},
                                            {'prefix': '88831', 'target_days': 31, 'remark': 'Maintain to 31 - 60 days'},
                                            {'prefix': '88861', 'target_days': 61, 'remark': 'Maintain to 61 - 90 days'},
                                            {'prefix': '88891', 'target_days': 91, 'remark': 'Maintain to 91+ days'}
                                        ]
                                        
                                        for config in configs:
                                            mask = merged_df['ADD Days'].astype(str).str.startswith(config['prefix'], na=False) & balance_positive_sql
                                            if mask.any():
                                                ref_asof = merged_df.loc[mask, 'AsOfDate'].dropna()
                                                target_due = (ref_asof.iloc[0] - pd.Timedelta(days=config['target_days'])) if not ref_asof.empty else None
                                                orig_due = merged_df.loc[mask, 'Original Due Date'] if 'Original Due Date' in merged_df.columns else merged_df.loc[mask, 'Due Date']
                                                orig_due = pd.to_datetime(orig_due, errors='coerce')
                                                merged_df.loc[mask, 'Due Date'] = merged_df.loc[mask, 'AsOfDate'] - pd.Timedelta(days=config['target_days'])
                                                if target_due is not None:
                                                    add_days_applied = (target_due - orig_due).dt.days.fillna(0).astype(int)
                                                    merged_df.loc[mask, 'ADD Days'] = add_days_applied.values
                                                else:
                                                    merged_df.loc[mask, 'ADD Days'] = 0
                                                remark_text = config['remark']
                                                merged_df.loc[mask, 'Remarks'] = merged_df.loc[mask, 'Remarks'].fillna('') + " | " + remark_text
                                        
                                        # This tab only: do NOT apply "Currently Listed Customer Names w/ Add-days" on initial load.
                                        # Recompute is from SQL tables only (same approach as "A/R with Add Days" modal but without the list).
                                        
                                        st.session_state.result_df6a = merged_df
                                        # merged_df.to_csv("debug_AR_merged_df_after_add_days_update.csv", index=False)  # Debugging line to check the content of merged_df    
                                                                        
                                st.session_state.display_df6s = st.session_state.result_df6a.copy()
                                st.session_state.display_df6s = st.session_state.display_df6s.drop_duplicates() 
                                
                                display_df6s_check = st.session_state.display_df6s.copy()                                
                                # display_df6s_check.to_csv("debug_AR_session_df6s_after_drop_duplicates.csv", index=False)  # Debugging line to check the content of merged_df    
                                
                                st.session_state.display_df6s['AsOfDate'] = pd.to_datetime(st.session_state.display_df6s['AsOfDate']) #Convert to date
                                st.session_state.display_df6s['Due Date'] = pd.to_datetime(st.session_state.display_df6s['Due Date']) #Convert to date
                                
                                st.session_state.display_df6s = update_calculations(st.session_state.display_df6s) #Update the Due Date based on ADD Days and Payment Terms

                                display_df6s_check = st.session_state.display_df6s.copy()  # noqa: F841
                                # display_df6s_check.to_csv("debug_AR_session_df6s_after_calculation.csv", index=False)  # Debugging line to check the content of merged_df    
                                                                
                                # st.session_state.display_df6s = st.session_state.display_df6s[                                                
                                #     (~st.session_state.display_df6s['Document No_'].fillna('').str.startswith(('PSCM', 'JV'))) & 
                                #     (~st.session_state.display_df6s['Journal Batch Name'].str.contains('EWT|WHT', na=False)) & 
                                #     (~st.session_state.display_df6s['Bal_ Account No_'].str.contains('EWT|WHT', na=False))
                                # ]   
                            
                            #$$$
                            if 'display_df6s' not in st.session_state:
                                st.session_state.display_df6s = pd.DataFrame()  # Replace with your actual DataFrame initialization
                                
                            ### Apply Re-Tagging before proceeding to fragments ###                        
                            ### NEED TO DO RETAGGING AFTER MERGE DF7 ### do this on module retagging_SR_names ###

                            # st.session_state.display_df6s = retagging_SR_names(st.session_state.display_df6s)                            
                            selectbox_fragments()
                                                        
                    with sstabz2: # Target COD 
                        with st.spinner("Target COD - Processing..."):                  
                            CR_TARGET_COD_fragment()
                              
                    with sstabz3: # CURRENT  
                        with st.spinner("Target CURRENT - Processing..."): 
                            CR_TARGET_CURRENT_fragment()                
                                    
                    with sstabz4:
                        with st.spinner("Target OVERDUE - Processing..."):
                            # st.subheader(f"Target OVERDUE AsOf AR [{date_from_str}]")
                            df_overdue = st.session_state.display_df6s.copy()
                            # Filter 'PSCM' and 'JV'
                            # df_overdue = df_overdue[(~df_overdue['Document No_'].fillna('').str.startswith(('PSCM', 'JV')))] 
                                                
                            overdue_sr_name = df_overdue[['SR2', 'Name','ADD Days','Due Date', 'Total Target', 'DOCUMENT TYPE','Current','Document No_']].copy() 
                            overdue_sr_name = overdue_sr_name[((overdue_sr_name['DOCUMENT TYPE'] == 'INVOICE') | (overdue_sr_name['DOCUMENT TYPE'] == '') | (overdue_sr_name['DOCUMENT TYPE'].isna())) & (overdue_sr_name['Current'].isna())]  # Filter for Document Type = 2                           
                            overdue_sr_name = overdue_sr_name.groupby(['SR2']).agg({'Total Target': 'sum'}).reset_index()
                            target_totals_od = overdue_sr_name['Total Target'].sum()   
                            overdue_sr_name = overdue_sr_name.rename(columns={'Total Target': 'Total Overdue Amount (PHP)'})                        
                            overdue_sr_name['Total Overdue Amount (PHP)'] = pd.to_numeric(overdue_sr_name['Total Overdue Amount (PHP)'], errors='coerce')  
                            overdue_sr_name['Total Overdue Amount (PHP)'] = overdue_sr_name['Total Overdue Amount (PHP)'].apply(lambda x: f"{x:,.2f}" if pd.notna(x) else None)                    
                            overdue_sr_name = overdue_sr_name.sort_values(by='SR2')  
                            
                            overdue_report = df_overdue.copy() 
                            overdue_report = overdue_report[((overdue_report['DOCUMENT TYPE'] == 'INVOICE') | (overdue_report['DOCUMENT TYPE'] == '') | (overdue_report['DOCUMENT TYPE'].isna())) & (overdue_report['Current'].isna())]  # Filter for Document Type = 2                    
                            # overdue_report = df_overdue.groupby(['SR2', 'Name']).agg({'Total Target': 'sum'}).reset_index()
                            overdue_report = overdue_report.rename(columns={'Total Target': 'Total Overdue Amount (PHP)'})
                            # overdue_report = overdue_report.sort_values(by='SR2')                    
                            # overdue_report.to_csv("debug_overdue_report.csv", index=False)  # Debugging line to check the content of overdue_report
                                                                                                                 
                            st.subheader(f"Target OVERDUE AsOf AR [{date_from_str}]  -  Total OVERDUE Amount: PHP {target_totals_od:,.2f}")
                            
                            target_totals_cnt = overdue_report['SR2'].count() 
                            
                            st.session_state.filtered_OVERDUE = overdue_sr_name.copy() 
                            st.session_state.overdue_report = overdue_report.copy()
                            
                            # SCR Name Summary
                            st.markdown(f"#### Overdue Report by SR Name ({target_totals_cnt})")                                                       
                            col_cfg_ov_sr = _numeric_column_config(overdue_sr_name)
                            st.dataframe(overdue_sr_name, use_container_width=True, hide_index=True, key='overdue_sr_name', column_config=col_cfg_ov_sr)
                            
                            csv = overdue_sr_name.to_csv(index=False)
                            st.download_button(
                                label="Download Overdue Report by SR Name",
                                data=csv,
                                key='overdue_sr_name',
                                file_name=f"Target OVERDUE SSR2 AsOf AR [{date_from_str}].csv",
                                mime="text/csv")
                            
                            # Details                        
                            st.markdown("#### Overdue Report by SR Name and Customer Name")                            
                            col_cfg_ov_rep = _numeric_column_config(overdue_report)
                            st.dataframe(overdue_report, use_container_width=True, hide_index=True, column_config=col_cfg_ov_rep)                                                                                                                                         
                                                        
                            # button to save the DataFrame as CSV
                            CR_btn_ov_fragment()
                                                   
                    with sstabz5:
                        # add
                        CR_TARGET_RETURNS_fragment()
                            
                    with sstabz6:
                        st_tab1_oa, st_tab2_oa = st.tabs(["Base on G/L Entries 1210", "Base on Cust Ledger Entries"])
                        with st.spinner("Target ADJUSTMENTS - Processing..."):
                            with st_tab1_oa:
                                st_tab1_entry, st_tab2_entry, st_tab3_entry, st_tab4_entry = st.tabs(["G/L Entries Summary (1210)", "DISCOUNT (DISC & SC)","TAX (WHT & EWT)","OTHERS (ADJ & BOUNCE)"])
                                with st_tab1_entry:
                                    st.subheader("G/L Entries Summary (1210)")
                                    CR_TARGET_OADJ_GL_fragment()
                                with st_tab2_entry:
                                    st.subheader("DISCOUNT (DISC & SC)") 
                                    CR_TARGET_OADJ_DISC_fragment()
                                with st_tab3_entry:
                                    st.subheader("TAX (WHT & EWT)") 
                                    CR_TARGET_OADJ_TAX_fragment()
                                with st_tab4_entry:
                                    st.subheader("OTHERS (ADJ & BOUNCE)") 
                                    CR_TARGET_OADJ_OTH_fragment()
                            
                            # Progress bar and status text
                            if st.session_state.bar_text is not None:
                                with mcol1:
                                    status_text.text("97%")
                                    st.session_state.status_text = "97%"
                                with mcol2:
                                    progress_bar.progress(97)   
                                                                    
                            with st_tab2_oa:
                                st.subheader("Other Adjustments Base on Customer Ledger Entries")                                                                                 
                                # Ensure result_df7 is available in session state SCR_NAME
                                CR_TARGET_OADJ_fragment()    
                    
                    
                    with sstabz7:
                        with st.spinner("Target Base Collection Performance Summary - Processing..."):
                            target_base_fragment()
                            # scroll_top() 
                                
                            # Progress bar and status text
                            if st.session_state.bar_text is not None:
                                with mcol1:
                                    status_text.text("98%")
                                with mcol2:
                                    progress_bar.progress(98)
##__________________________________________________________________________________________________________________##                    
                        
                with tabs3:
                    # st.title("SUMMARY GENERATED REPORT")       
                    st.markdown(""" <h1 style="color: #6b3fa0; font-weight: bold; text-align: left; text-shadow: 2px 2px 4px rgba(0,0,0,0.3);">
                                        SUMMARY GENERATED REPORT
                                    </h1>
                                """, unsafe_allow_html=True) 
                                                     
                    with st.spinner("Collection Performance Target with Category - Processing..."):
                        current_df_t = st.session_state.filtered_CURRENT_details.copy()
                        overdue_df_t = st.session_state.display_df6s.copy()
                                                    
                        current_df_t = current_df_t[['DocumentNo', 'ExternalDocumentNo','CustomerNo','SCR_NAME','DetailDate','DetailDoc','DetailAmount','Collected_Amount','Collected_EWT']]                            
                        
                        main_merge = pd.merge(overdue_df_t, current_df_t, left_on=['Document No_', 'External Document No_', 'Customer No_'], right_on=['DocumentNo', 'ExternalDocumentNo','CustomerNo'], how='left')
                        
                        # Conditions for Overdue and Current
                        main_df = main_merge.copy() # DF for Overdue and Current
                        second_df = pd.read_csv('CO_Conditions.csv') # Target Category Conditions (OVERDUE AND CURRENT) from CSV File                                                    
                        
                        # OVERDUE
                        result_df_co = target_category_fragment(main_df,second_df, target='CO')                            
                        # CURRENT
                        result_df_cur = target_category_fragment(main_df,second_df, target='CUR')                            
                        # COD
                        main_df = st.session_state.filtered_COD_details.copy() # special df for COD
                        second_df = pd.read_csv('COD_Conditions.csv') # special conditions for COD                       
                        result_df_cod = target_category_fragment(main_df,second_df, target='COD')                                                        
                                                                            
                        #### SUMMARY OF 3 TABLES ####
                        mask_overdue = result_df_co['Collected_Amount'].isna() | (result_df_co['Collected_Amount'] == '')
                        target_overdue = result_df_co.loc[mask_overdue, ['Target_Category_Name', 'DSS2_Name', 'SR2', 'Total Target']].copy()

                        collected_numeric = pd.to_numeric(result_df_cur['Collected_Amount'], errors='coerce')
                        mask_current = (~result_df_cur['Collected_Amount'].isna()) & (result_df_cur['Collected_Amount'] != '') & (collected_numeric != 0)
                        target_current = result_df_cur.loc[mask_current, ['Target_Category_Name', 'DSS2_Name', 'SCR_NAME', 'Collected_Amount']].copy()                                                        
                        target_cod = result_df_cod[['Target_Category_Name','DSS2_Name','SCR_NAME','Collected_Amount']].copy()
                        
                        target_overdue = target_overdue.rename(columns={'SR2': 'SCR_NAME', 'Total Target': 'Overdue_Amount'})
                        target_current = target_current.rename(columns={'Collected_Amount': 'Current_Amount'})
                        target_cod = target_cod.rename(columns={'Collected_Amount': 'COD_Amount'})
                        
                        target_overdue['Overdue_Amount'] = pd.to_numeric(target_overdue['Overdue_Amount'], errors='coerce').fillna(0)
                        target_current['Current_Amount'] = pd.to_numeric(target_current['Current_Amount'], errors='coerce').fillna(0)
                        target_cod['COD_Amount'] = pd.to_numeric(target_cod['COD_Amount'], errors='coerce').fillna(0)
                        
                        grouped_overdue = target_overdue.groupby(['Target_Category_Name', 'DSS2_Name', 'SCR_NAME'], as_index=False)['Overdue_Amount'].sum()
                        grouped_current = target_current.groupby(['Target_Category_Name', 'DSS2_Name', 'SCR_NAME'], as_index=False)['Current_Amount'].sum()
                        grouped_cod = target_cod.groupby(['Target_Category_Name', 'DSS2_Name', 'SCR_NAME'], as_index=False)['COD_Amount'].sum()                            
                        
                        
                        # DATAFRAMES for display                            
                        st.subheader("Target base w/ category - summary of OVERDUE, CURRENT, COD")
                        target_category_group_fragment(grouped_overdue, grouped_current, grouped_cod)
                        
                        st.session_state.result_df_co = result_df_co.copy()
                        st.session_state.result_df_cur = result_df_cur.copy()   
                        st.session_state.result_df_cod = result_df_cod.copy()
                        
                        # Checkbox to show details DataFrame
                        show_details_df_fragment()
                        
                        # Progress bar and status text
                        if st.session_state.bar_text is not None:
                            with mcol1:
                                status_text.text("99%")
                            with mcol2:
                                progress_bar.progress(99)   
                                                                                                                                          
                with tabs4:
                    tab_oth1, tab_oth2, tab_oth3 = st.tabs(["Unadjusted / Uncorrected Remaining Balance", "Overdue Summary","DSS Collection Summary Report"])
                    with tab_oth1:
                        st.markdown("#### Not Yet Due Remaining Balance")
                        result_df_rb = st.session_state.result_df 
                        required_display_columns = ['inv_dr_date','inv_dr_no','customer_name', 'remaining_balance', 'suggestion_remarks']           
                        filtered_df = result_df_rb[result_df_rb['remaining_balance'] != 0][required_display_columns]
                        
                        # Sort by remaining_balance
                        filtered_df = filtered_df.sort_values(by='suggestion_remarks', ascending=False)
                        
                        # Display the table
                        col_cfg_rb = _numeric_column_config(filtered_df)
                        st.dataframe(filtered_df, use_container_width=True, hide_index=True, key='remaining_balance_remarks', column_config=col_cfg_rb)  
                                                                              
                    with tab_oth2:
                        TOverdue_fragment()    
                                       
                    with tab_oth3:
                        with st.spinner("DSS Collection Summary - Processing..."):
                            st_result_df = st.session_state.result_df7.copy() #st.session_state.result_df
                            st_result_df.columns = st_result_df.columns.str.lower()
                            
                            st.markdown("#### Filter by DSS with Collection Results")
                            
                            # Compute total remaining balance, dept, and dept_code per DSM
                            dss_summary = st_result_df.groupby(['dss_name']).agg({'remaining balance': 'sum'}).reset_index()
                            dss_summary = dss_summary[dss_summary['remaining balance'] > 0].groupby(['dss_name']).agg({
                                'remaining balance': 'sum',
                            }).reset_index()
                            
                            # Sort dsm_summary by dept_code
                            dss_summary = dss_summary.sort_values(by=['dss_name'])
                            
                            # Create custom labels for the selectbox, preserving the sorted order
                            dss_options = ['All'] + [f"{row['dss_name']} - (Remaining Balance: PHP {row['remaining balance']:,.2f})" 
                                                for _, row in dss_summary.iterrows()]
                            
                            # Use selectbox to update the state
                            selected_dss = st.selectbox("Select DSS", dss_options, key="dss_selectbox")
                            st.session_state.selected_dss = selected_dss

                            # Extract the selected DSS value and dept_code from the label
                            selected_dss_value = selected_dss if selected_dss == 'All' else selected_dss.split(' - (')[0]
                            # st_selected_dept_value = None if selected_dss == 'All' else selected_dss.split('Dept: ')[1].rstrip(')')
                            
                            # Filter data based on DSS selection using the stored st_result_df
                            if selected_dss_value == 'All':
                                st_filtered_df = st_result_df[st_result_df['remaining balance'].notna() & (st_result_df['remaining balance'] != 0)]
                            else:
                                st_filtered_df = st_result_df[(st_result_df['dss_name'] == selected_dss_value) & 
                                                    st_result_df['remaining balance'].notna() & 
                                                    (st_result_df['remaining balance'] != 0)]
                                
                            # Select columns for display: cr, customer_name, remaining balance, and dynamic columns after detaildate
                            base_columns = ['scr_name', 'customername']
                            display_columns = base_columns + ['remaining balance']
                            # dynamic_columns = [col for col in result_df.columns[result_df.columns.get_loc('detaildate') + 1:]
                            #                 if re.match(r'^\d{2}-[a-z]{3}$', col) or col.startswith('%_')]
                            # display_columns.extend(dynamic_columns)

                            # Ensure only available columns are selected
                            display_columns = [col for col in display_columns if col in st_filtered_df.columns]

                            # Group by cr and customer_name, summing the dynamic columns and remaining balance
                            group_columns = ['scr_name', 'customername']
                            agg_dict = {'remaining balance': 'sum'}
                            for col in dynamic_columns:
                                if col in st_filtered_df.columns:
                                    agg_dict[col] = 'sum'
                            
                            st_filtered_df = st_filtered_df.groupby(group_columns).agg(agg_dict).reset_index()

                            # Reorder columns to match display_columns
                            st_filtered_df = st_filtered_df[display_columns]

                            # Sort by cr and customer_name
                            st_filtered_df = st_filtered_df.sort_values(by=['scr_name', 'customername'])
                                
                            # Display filtered table
                            if not st_filtered_df.empty:
                                # Create a display copy of your DataFrame
                                st_filtered_display_df = st_filtered_df.copy()

                                col_cfg_st = _numeric_column_config(st_filtered_display_df)
                                st.dataframe(st_filtered_display_df, use_container_width=True, hide_index=True, column_config=col_cfg_st)        
                                
                                # CSV download button for filtered data
                                st_filtered_csv = st_filtered_df.to_csv(index=False)
                                st.download_button(
                                    label="Download DSS Collection Summary Report",
                                    data=st_filtered_csv,
                                    file_name=f"DSS_{selected_dss_value}_Summary_Report_with_CR_Collection As-Of {asof_month}.csv",
                                    mime="text/csv"
                                )
                            else:
                                st.info("No data available for the selected DSS with non-zero remaining balance.")
                        
        if st.session_state.bar_text is not None:           
            mcol1, mcol2 = st.columns([1, 15])    
            with mcol1:
                status_text.empty()
                
            with mcol2:
                progress_bar.empty()
            st.session_state.bar_text = None  
        scroll_top()
    # scroll_top()                
#++#############################################################################################################################################################################        
#++#############################################################################################################################################################################        
             
                        
                         
############################################################################################################################################################################################################################################################################################################################################################################################################################################################################################################################
# Main execution
print("Starting main execution")

if not st.session_state.authenticated or st.session_state.username is None or st.session_state.access_level is None:
    print("User not authenticated, showing login form")
    
    login_form()
else:
    print("User authenticated, proceeding to main_app")
    
    main_app()
##########
# Version 3 (09-04-2025)
##########
# if __name__ == "__main__":
#     print("Running __main__")
#     if not st.session_state.authenticated or st.session_state.username is None or st.session_state.access_level is None:
#         login_form()
#     else:
#         main_app()
