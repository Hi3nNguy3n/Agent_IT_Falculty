import os
import difflib
import unicodedata
from typing import Dict, Optional

import pandas as pd
import streamlit as st


def _normalize_text(s: str) -> str:
    s = str(s or "").strip().lower().replace("\n", " ")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(c for c in s if c.isalnum() or c.isspace())
    s = " ".join(s.split())
    return s


TARGETS = [
    "mon hoc",
    "ten giang vien",
    "email",
    "lop",
    "ngay bat dau",
    "ngay ket thuc",
    "co so hoc",
    "phong hoc",
]

SYNONYMS: Dict[str, set] = {
    "mon hoc": {"mon hoc", "ten mon", "ten mon hoc", "mon"},
    "ten giang vien": {"ten giang vien", "giang vien", "gv", "ten gv"},
    "email": {"email", "e mail", "mail", "gmail", "thu dien tu"},
    "lop": {"lop", "ten lop", "ma lop", "nhom lop"},
    "ngay bat dau": {"ngay bat dau", "ngay bd", "bat dau"},
    "ngay ket thuc": {"ngay ket thuc", "ngay kt", "ket thuc"},
    "co so hoc": {"co so hoc", "co so", "cs", "co so dao tao"},
    "phong hoc": {"phong hoc", "phong", "ph"},
}


def _find_column_mapping(df: pd.DataFrame) -> Dict[str, Optional[str]]:
    norm_to_orig = {_normalize_text(c): c for c in df.columns}
    mapping: Dict[str, Optional[str]] = {t: None for t in TARGETS}
    for target in TARGETS:
        cands = SYNONYMS.get(target, {target}) | {target}
        # 1) exact normalized
        for k in cands:
            if k in norm_to_orig:
                mapping[target] = norm_to_orig[k]
                break
        if mapping[target]:
            continue
        # 2) containment by words
        for k in cands:
            words = [w for w in k.split() if w]
            for norm_col, orig_col in norm_to_orig.items():
                hay = f" {norm_col} "
                if all(f" {w} " in hay for w in words):
                    mapping[target] = orig_col
                    break
            if mapping[target]:
                break
        if mapping[target]:
            continue
        # 3) fuzzy
        best_col = None
        best_score = 0.0
        for norm_col, orig_col in norm_to_orig.items():
            for k in cands:
                score = difflib.SequenceMatcher(None, k, norm_col).ratio()
                if score > best_score:
                    best_score = score
                    best_col = orig_col
        mapping[target] = best_col if (best_col and best_score >= 0.68) else None
    return mapping


def _fmt_date(val) -> Optional[str]:
    if pd.isna(val):
        return None
    try:
        d = pd.to_datetime(val, errors="coerce").date()
        return d.strftime("%d/%m/%Y") if d else None
    except Exception:
        return None


def _join_unique(series: pd.Series) -> str:
    vals = []
    for v in series:
        if pd.isna(v):
            continue
        s = str(v).strip()
        if not s:
            continue
        parts = [p.strip() for p in s.split(",")]
        for p in parts:
            if p and p not in vals:
                vals.append(p)
    return ", ".join(vals)


def _join_unique_dates(series: pd.Series) -> str:
    vals = []
    for v in series:
        s = _fmt_date(v)
        if s and s not in vals:
            vals.append(s)
    # sort dd/mm/yyyy if possible
    try:
        vals = sorted(vals, key=lambda x: (int(x[6:10]), int(x[3:5]), int(x[0:2])))
    except Exception:
        pass
    return ", ".join(vals)


st.set_page_config(page_title="Mon theo giang vien", layout="wide")
st.title("Xem mon hoc theo giang vien")

with st.sidebar:
    default_path = "file_data.xlsx"
    sheet_name = st.text_input("Ten sheet", value="TKB_HK2_Moi_Giang")
    header_row_excel = st.number_input("Hang header (Excel)", min_value=1, value=2, step=1)
    uploaded = st.file_uploader("Tai len file Excel", type=["xlsx"])

def load_df():
    try:
        if uploaded is not None:
            df = pd.read_excel(uploaded, sheet_name=sheet_name, header=int(header_row_excel) - 1)
            st.caption(f"Doc tu file tai len: {uploaded.name}")
        else:
            df = pd.read_excel(default_path, sheet_name=sheet_name, header=int(header_row_excel) - 1)
            st.caption(f"Doc tu '{default_path}'")
    except Exception as e:
        st.error(f"Khong the doc Excel: {e}")
        return None
    return df

df = load_df()
if df is not None:
    mapping = _find_column_mapping(df)
    col_gv = mapping.get("ten giang vien")
    col_mon = mapping.get("mon hoc")
    col_lop = mapping.get("lop")
    col_bd = mapping.get("ngay bat dau")
    col_kt = mapping.get("ngay ket thuc")
    col_coso = mapping.get("co so hoc")
    col_phong = mapping.get("phong hoc")

    if not col_gv or not col_mon:
        st.warning("Thieu cot 'ten giang vien' hoac 'mon hoc'")
    else:
        # forward fill merged cells for reliable grouping
        for c in [col_gv, col_mon, col_lop, col_bd, col_kt, col_coso, col_phong]:
            if c in df.columns:
                df[c] = df[c].ffill()

        # format dates to dd/mm/yyyy strings
        if col_bd in df.columns:
            df[col_bd] = df[col_bd].map(_fmt_date)
        if col_kt in df.columns:
            df[col_kt] = df[col_kt].map(_fmt_date)

        teachers = sorted([str(x) for x in df[col_gv].dropna().unique().tolist() if str(x).strip()], key=lambda s: s.lower())
        selected = st.selectbox("Chon giang vien", options=teachers)
        mode = st.radio("Kieu hien thi", options=["Moi mon 1 hang", "Tach theo ngay"], index=0, horizontal=True)

        view = pd.DataFrame()
        if selected:
            sub = df[df[col_gv] == selected].copy()
            split_by_day = (mode == "Tach theo ngay")
            if split_by_day:
                group_keys = [k for k in [col_mon, col_bd, col_kt] if k]
            else:
                group_keys = [k for k in [col_mon] if k]
            agg_map: Dict[str, callable] = {}
            for c in [col_lop, col_coso, col_phong]:
                if c:
                    agg_map[c] = _join_unique
            if not split_by_day:
                # Summarize dates as unique lists when not splitting by day
                if col_bd:
                    agg_map[col_bd] = _join_unique_dates
                if col_kt:
                    agg_map[col_kt] = _join_unique_dates
            if agg_map:
                view = sub.groupby(group_keys, as_index=False).agg(agg_map)
            else:
                view = sub[group_keys].drop_duplicates()

            rename = {}
            if col_mon: rename[col_mon] = "Mon hoc"
            if col_lop: rename[col_lop] = "Lop"
            if col_bd: rename[col_bd] = "Ngay bat dau"
            if col_kt: rename[col_kt] = "Ngay ket thuc"
            if col_coso: rename[col_coso] = "Co so"
            if col_phong: rename[col_phong] = "Phong hoc"
            view = view.rename(columns=rename)
            order_cols = [c for c in ["Mon hoc", "Ngay bat dau", "Ngay ket thuc", "Lop", "Co so", "Phong hoc"] if c in view.columns]
            view = view[order_cols]

        st.subheader("Ket qua")
        st.dataframe(view, use_container_width=True)

        # allow CSV export for selected teacher
        if not view.empty:
            csv_bytes = view.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
            st.download_button(
                "Tai ve CSV (theo GV)",
                data=csv_bytes,
                file_name=f"mon_hoc_{_normalize_text(selected) or 'gv'}.csv",
                mime="text/csv",
            )
