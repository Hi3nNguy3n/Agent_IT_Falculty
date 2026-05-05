import argparse
import os
import difflib
import unicodedata
from typing import Dict, Optional, Iterable

import pandas as pd


def _normalize_text(s: str) -> str:
    s = str(s or "").strip().lower()
    s = s.replace("\n", " ")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    # keep only alphanumerics and spaces
    s = "".join(c for c in s if c.isalnum() or c.isspace())
    s = " ".join(s.split())
    return s


# Targets we care about
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


def find_column_mapping(df: pd.DataFrame) -> Dict[str, Optional[str]]:
    norm_to_orig = {_normalize_text(c): c for c in df.columns}

    mapping: Dict[str, Optional[str]] = {t: None for t in TARGETS}
    for target in TARGETS:
        cands = SYNONYMS.get(target, {target}) | {target}

        # 1) exact normalized match
        for key in cands:
            if key in norm_to_orig:
                mapping[target] = norm_to_orig[key]
                break
        if mapping[target]:
            continue
        # 2) containment by words
        for key in cands:
            words = [w for w in key.split() if w]
            for norm_col, orig_col in norm_to_orig.items():
                hay = f" {norm_col} "
                if all(f" {w} " in hay for w in words):
                    mapping[target] = orig_col
                    break
            if mapping[target]:
                break
        if mapping[target]:
            continue
        # 3) fuzzy best
        best_col = None
        best_score = 0.0
        for norm_col, orig_col in norm_to_orig.items():
            for key in cands:
                score = difflib.SequenceMatcher(None, key, norm_col).ratio()
                if score > best_score:
                    best_score = score
                    best_col = orig_col
        mapping[target] = best_col if (best_col and best_score >= 0.68) else None
    return mapping


def _clean_items(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for it in items:
        s = (it or "").strip()
        if not s:
            continue
        # skip obvious Excel error tokens
        if s.upper() in {"#VALUE!", "#N/A", "NAN", "NONE"}:
            continue
        if s not in out:
            out.append(s)
    return out


def join_unique(series: pd.Series) -> str:
    vals: list[str] = []
    for v in series:
        if pd.isna(v):
            continue
        s = str(v)
        parts = [p.strip() for p in s.split(",")]
        vals.extend(p for p in parts if p)
    vals = _clean_items(vals)
    return ", ".join(vals)


def _format_date(v) -> Optional[str]:
    try:
        dt = pd.to_datetime(v, errors="coerce")
        if pd.isna(dt):
            return None
        if isinstance(dt, pd.Timestamp):
            d = dt.date()
        else:
            d = dt
        return d.strftime("%d/%m/%Y")
    except Exception:
        return None


def join_unique_dates(series: pd.Series) -> str:
    vals: list[str] = []
    for v in series:
        s = _format_date(v)
        if s:
            vals.append(s)
    vals = _clean_items(vals)
    # try to sort by dd/mm/yyyy
    try:
        vals = sorted(vals, key=lambda x: (int(x[6:10]), int(x[3:5]), int(x[0:2])))
    except Exception:
        pass
    return ", ".join(vals)


def build_teacher_summary(df: pd.DataFrame, mapping: Dict[str, Optional[str]]) -> pd.DataFrame:
    col_mon = mapping.get("mon hoc")
    col_gv = mapping.get("ten giang vien")
    if not col_gv:
        raise ValueError("Khong tim thay cot 'ten giang vien' trong file")

    col_email = mapping.get("email")
    col_lop = mapping.get("lop")
    col_bd = mapping.get("ngay bat dau")
    col_kt = mapping.get("ngay ket thuc")
    col_coso = mapping.get("co so hoc")
    col_phong = mapping.get("phong hoc")

    group_keys = [col_gv] + ([col_email] if col_email else [])

    agg_map: Dict[str, callable] = {}
    for c in [col_mon, col_lop, col_coso, col_phong]:
        if c:
            agg_map[c] = join_unique
    for c in [col_bd, col_kt]:
        if c:
            agg_map[c] = join_unique_dates

    tmp = df.copy()
    # Fill forward merged cells for teacher/email and replace NaN with empty before grouping
    tmp[col_gv] = tmp[col_gv].ffill()
    if col_email:
        tmp[col_email] = tmp[col_email].ffill()
    for c in agg_map.keys():
        tmp[c] = tmp[c].fillna("")

    if agg_map:
        out = tmp.groupby(group_keys, as_index=False).agg(agg_map)
    else:
        out = tmp[group_keys].drop_duplicates()

    rename_map = {}
    rename_map[col_gv] = "Giang vien"
    if col_mon:
        rename_map[col_mon] = "Mon hoc"
    if col_email:
        rename_map[col_email] = "Email"
    if col_lop:
        rename_map[col_lop] = "Lop"
    if col_bd:
        rename_map[col_bd] = "Ngay bat dau"
    if col_kt:
        rename_map[col_kt] = "Ngay ket thuc"
    if col_coso:
        rename_map[col_coso] = "Co so"
    if col_phong:
        rename_map[col_phong] = "Phong hoc"

    out = out.rename(columns=rename_map)
    preferred = [
        "Giang vien",
        "Mon hoc",
        "Email",
        "Lop",
        "Ngay bat dau",
        "Ngay ket thuc",
        "Co so",
        "Phong hoc",
    ]
    out = out[[c for c in preferred if c in out.columns]]
    # sort by teacher name
    if "Giang vien" in out.columns:
        out = out.sort_values(by=["Giang vien"]).reset_index(drop=True)
    return out


def build_subject_summary(df: pd.DataFrame, mapping: Dict[str, Optional[str]]) -> pd.DataFrame:
    col_mon = mapping.get("mon hoc")
    col_gv = mapping.get("ten giang vien")
    if not col_mon:
        raise ValueError("Khong tim thay cot 'mon hoc' trong file")
    if not col_gv:
        raise ValueError("Khong tim thay cot 'ten giang vien' trong file")

    col_email = mapping.get("email")
    col_lop = mapping.get("lop")
    col_bd = mapping.get("ngay bat dau")
    col_kt = mapping.get("ngay ket thuc")
    col_coso = mapping.get("co so hoc")
    col_phong = mapping.get("phong hoc")

    tmp = df.copy()
    # Forward fill possible merged header cells
    tmp[col_mon] = tmp[col_mon].ffill()
    tmp[col_gv] = tmp[col_gv].ffill()
    if col_email:
        tmp[col_email] = tmp[col_email].ffill()

    # Normalize dates to dd/mm/yyyy strings before grouping
    if col_bd and col_bd in tmp.columns:
        tmp[col_bd] = tmp[col_bd].map(_format_date)
    if col_kt and col_kt in tmp.columns:
        tmp[col_kt] = tmp[col_kt].map(_format_date)

    # Build grouping keys: subject + teacher + date range + campus + room (split rows per combo)
    group_keys = [k for k in [col_mon, col_gv, col_bd, col_kt, col_coso, col_phong] if k]

    # Aggregate other fields (do NOT group by class code, but classes join-unique)
    agg_map: Dict[str, callable] = {}
    for c in [col_email, col_lop]:
        if c:
            agg_map[c] = join_unique

    for c in agg_map.keys():
        if c in tmp.columns:
            tmp[c] = tmp[c].fillna("")

    out = (
        tmp.groupby(group_keys, as_index=False).agg(agg_map)
        if agg_map else tmp[group_keys].drop_duplicates()
    )

    # Rename for display
    rename_map = {
        col_mon: "Mon hoc",
        col_gv: "Giang vien",
    }
    if col_email:
        rename_map[col_email] = "Email"
    if col_lop:
        rename_map[col_lop] = "Lop"
    if col_bd:
        rename_map[col_bd] = "Ngay bat dau"
    if col_kt:
        rename_map[col_kt] = "Ngay ket thuc"
    if col_coso:
        rename_map[col_coso] = "Co so"
    if col_phong:
        rename_map[col_phong] = "Phong hoc"

    out = out.rename(columns=rename_map)
    preferred = [
        "Mon hoc",
        "Giang vien",
        "Email",
        "Lop",
        "Ngay bat dau",
        "Ngay ket thuc",
        "Co so",
        "Phong hoc",
    ]
    out = out[[c for c in preferred if c in out.columns]]
    # Sort for readability: subject, teacher, start date
    sort_cols = [c for c in ["Mon hoc", "Giang vien", "Ngay bat dau", "Ngay ket thuc"] if c in out.columns]
    if sort_cols:
        out = out.sort_values(by=sort_cols).reset_index(drop=True)
    return out


def build_class_summary(df: pd.DataFrame, mapping: Dict[str, Optional[str]]) -> pd.DataFrame:
    col_lop = mapping.get("lop")
    if not col_lop:
        raise ValueError("Khong tim thay cot 'lop' trong file")

    col_mon = mapping.get("mon hoc")
    col_gv = mapping.get("ten giang vien")
    col_email = mapping.get("email")
    col_bd = mapping.get("ngay bat dau")
    col_kt = mapping.get("ngay ket thuc")
    col_coso = mapping.get("co so hoc")
    col_phong = mapping.get("phong hoc")

    tmp = df.copy()
    # ffill for reliable grouping
    for c in [col_lop, col_mon, col_gv, col_bd, col_kt, col_coso, col_phong]:
        if c and c in tmp.columns:
            tmp[c] = tmp[c].ffill()
    
    if col_email and col_email in tmp.columns:
        tmp[col_email] = tmp[col_email].ffill()

    # Normalize dates
    if col_bd and col_bd in tmp.columns:
        tmp[col_bd] = tmp[col_bd].map(_format_date)
    if col_kt and col_kt in tmp.columns:
        tmp[col_kt] = tmp[col_kt].map(_format_date)

    # Group keys: Class + Subject + Days + Times
    group_keys = [k for k in [col_lop, col_mon, col_bd, col_kt, col_coso, col_phong] if k]

    agg_map: Dict[str, callable] = {}
    if col_gv: agg_map[col_gv] = join_unique
    if col_email: agg_map[col_email] = join_unique

    for c in agg_map.keys():
        tmp[c] = tmp[c].fillna("")

    out = (
        tmp.groupby(group_keys, as_index=False).agg(agg_map)
        if agg_map else tmp[group_keys].drop_duplicates()
    )

    # Rename
    rename_map = {col_lop: "Lop"}
    if col_mon: rename_map[col_mon] = "Mon hoc"
    if col_gv: rename_map[col_gv] = "Giang vien"
    if col_email: rename_map[col_email] = "Email"
    if col_bd: rename_map[col_bd] = "Ngay bat dau"
    if col_kt: rename_map[col_kt] = "Ngay ket thuc"
    if col_coso: rename_map[col_coso] = "Co so"
    if col_phong: rename_map[col_phong] = "Phong hoc"

    out = out.rename(columns=rename_map)
    preferred = ["Lop", "Mon hoc", "Giang vien", "Email", "Ngay bat dau", "Ngay ket thuc", "Co so", "Phong hoc"]
    out = out[[c for c in preferred if c in out.columns]]
    
    if "Lop" in out.columns:
        out = out.sort_values(by=["Lop", "Mon hoc"] if "Mon hoc" in out.columns else ["Lop"]).reset_index(drop=True)
    return out


def main():
    parser = argparse.ArgumentParser(description="Tao file tong hop tu Excel TKB")
    parser.add_argument("-i", "--input", default="datanew.xlsx", help="Duong dan file Excel dau vao")
    parser.add_argument("-s", "--sheet", default="TKB_HK3_Moi_Giang", help="Ten sheet")
    parser.add_argument("-H", "--header-row", type=int, default=2, help="Hang header trong Excel (1-based)")
    parser.add_argument("-o", "--output", default="mon_hoc_tach_theo_ngay.csv", help="Duong dan file dau ra (.csv hoac .xlsx)")
    parser.add_argument("-m", "--mode", choices=["teacher", "subject", "class"], default="subject", help="Kieu tong hop")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        raise SystemExit(f"Khong tim thay file: {args.input}")

    # pandas: header is 0-based
    header_idx = max(0, args.header_row - 1)
    df = pd.read_excel(args.input, sheet_name=args.sheet, header=header_idx)

    mapping = find_column_mapping(df)

    # Loại bỏ các hàng có phòng là "Tự học"
    col_phong = mapping.get("phong hoc")
    if col_phong and col_phong in df.columns:
        mask_tu_hoc = df[col_phong].apply(lambda x: _normalize_text(x) == "tu hoc")
        df = df[~mask_tu_hoc].reset_index(drop=True)

    if args.mode == "subject":
        out = build_subject_summary(df, mapping)
    elif args.mode == "teacher":
        out = build_teacher_summary(df, mapping)
    else:
        out = build_class_summary(df, mapping)

    # Write output depending on extension (CSV default)
    ext = os.path.splitext(args.output)[1].lower()
    out_path = args.output
    if not ext:
        out_path = args.output + ".csv"
        ext = ".csv"

    if ext == ".csv":
        out.to_csv(out_path, index=False, encoding="utf-8-sig")
        print(f"Da tao: {out_path}")
    elif ext in (".xlsx", ".xlsm", ".xltx", ".xltm"):
        with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
            out.to_excel(writer, index=False, sheet_name="GiangVien")
        print(f"Da tao: {out_path}")
    else:
        csv_path = os.path.splitext(args.output)[0] + ".csv"
        out.to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(f"Khong ho tro dinh dang {ext}, da tao CSV: {csv_path}")


if __name__ == "__main__":
    main()
