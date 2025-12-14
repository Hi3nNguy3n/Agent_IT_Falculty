import io
from datetime import datetime, time, timedelta
import unicodedata
import difflib
import re

import pandas as pd
import streamlit as st


# -----------------------------
# Helpers
# -----------------------------
def _normalize_text(s: str) -> str:
    s = str(s or "").strip().lower()
    s = s.replace("\n", " ")
    s = s.replace("Đ", "D").replace("đ", "d")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    # remove punctuation/symbols except spaces and alphanumerics
    s = "".join(c for c in s if c.isalnum() or c.isspace())
    s = " ".join(s.split())
    return s


TARGET_DISPLAY = {
    "mon hoc": "Môn Học",
    "lop": "Lớp",
    "ten giang vien": "Tên Giảng Viên",
    "ngay bat dau": "Ngày Bắt Đầu",
    "ngay ket thuc": "Ngày Kết Thúc",
    "thoi gian bd": "Thời Gian BĐ",
    "thoi gian kt": "Thời gian KT",
    "thu": "Thứ",
    "phong hoc": "Phòng Học",
    "co so hoc": "Cơ sở học",
}

SYNONYMS = {
    "mon hoc": {
        "mon hoc",
        "ten mon",
        "ten mon hoc",
        # degraded headers sometimes seen
        "mon ho",
        "on hoc",
        "on ho",
    },
    "lop": {"lop", "ten lop", "ma lop"},
    "ten giang vien": {
        "ten giang vien",
        "giang vien",
        "gv",
        # truncated variants
        "ten giang vie",
        "en giang vie",
    },
    "ngay bat dau": {
        "ngay bat dau",
        "ngay bd",
        "bat dau",
        # truncated
        "ngay bat da",
    },
    "ngay ket thuc": {
        "ngay ket thuc",
        "ngay kt",
        "ket thuc",
        # truncated
        "ngay ket thu",
    },
    "thoi gian bd": {
        "thoi gian bd",
        "gio bd",
        "tg bd",
        "thoi gian bat dau",
        "gio bat dau",
        # multi-line/truncated
        "thoi gian b d",
    },
    "thoi gian kt": {
        "thoi gian kt",
        "gio kt",
        "tg kt",
        "thoi gian ket thuc",
        "gio ket thuc",
        # multi-line/truncated
        "thoi gian k t",
    },
    "thu": {"thu", "thu trong tuan", "day"},
    "phong hoc": {
        "phong hoc",
        "phong",
        "phong hoc room",
        # truncated
        "phong ho",
    },
    "co so hoc": {
        "co so hoc",
        "co so",
        "co so day",
        "co so dao tao",
        "cs",
        # truncated
        "co so ho",
        "o so ho",
    },
}


def _find_column_mapping(df_columns):
    # Map normalized df columns to original names
    norm_to_orig = {_normalize_text(c): c for c in df_columns}
    mapping_display_to_source = {}

    for target_norm, display_name in TARGET_DISPLAY.items():
        candidates = SYNONYMS.get(target_norm, {target_norm})
        found = None
        # 1) exact normalized match
        for key in candidates:
            if key in norm_to_orig:
                found = norm_to_orig[key]
                break
        if not found:
            # 2) loose matching: whole-word containment for each synonym
            for key in candidates:
                words = key.split()
                for norm_col, orig_col in norm_to_orig.items():
                    hay = f" {norm_col} "
                    if all(f" {w} " in hay for w in words if w):
                        found = orig_col
                        break
                if found:
                    break
        if found:
            mapping_display_to_source[display_name] = found
            continue

        # 3) fuzzy fallback (skip very short targets to avoid false positives)
        def _should_fuzzy(t: str) -> bool:
            return len(t.replace(" ", "")) >= 4

        if not found and _should_fuzzy(target_norm):
            best_col = None
            best_score = 0.0
            for norm_col, orig_col in norm_to_orig.items():
                # compare against the best of synonyms
                for key in candidates:
                    score = difflib.SequenceMatcher(None, key, norm_col).ratio()
                    if score > best_score:
                        best_score = score
                        best_col = orig_col
            if best_col and best_score >= 0.68:
                mapping_display_to_source[display_name] = best_col

    return mapping_display_to_source


def _excel_time_to_time(val):
    # Handle mixed time representations from Excel
    if pd.isna(val):
        return None
    if isinstance(val, time):
        return val
    if isinstance(val, datetime):
        return val.time()
    if isinstance(val, (int, float)):
        # Excel times can be stored as fraction of a day
        fraction = float(val) % 1.0
        seconds = round(fraction * 24 * 60 * 60)
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return time(hour=h, minute=m, second=s)
    # Try generic parse
    try:
        dt = pd.to_datetime(val, errors="coerce")
        if pd.isna(dt):
            return None
        if isinstance(dt, pd.Timestamp):
            return dt.time()
        if isinstance(dt, datetime):
            return dt.time()
    except Exception:
        return None
    return None


def _fmt_date(val):
    if pd.isna(val):
        return None
    try:
        d = pd.to_datetime(val, errors="coerce").date()
        return d.strftime("%d/%m/%Y") if d else None
    except Exception:
        return None


def _fmt_time(val):
    if not val:
        return None
    if isinstance(val, time):
        return val.strftime("%H:%M")
    return None


# -----------------------------
# Heuristics for auto column guess
# -----------------------------
TIME_RE = re.compile(r"^\s*\d{1,2}:\d{2}(:\d{2})?\s*$")
DATE_RE = re.compile(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}")


def _is_time_like_value(v) -> bool:
    if pd.isna(v):
        return False
    if isinstance(v, time):
        return True
    if isinstance(v, datetime):
        return True
    if isinstance(v, (int, float)):
        # Excel time typically < 1
        return 0 <= float(v) < 1.0
    s = str(v).strip()
    if TIME_RE.match(s):
        return True
    return False


def _is_date_like_value(v) -> bool:
    if pd.isna(v):
        return False
    if isinstance(v, datetime):
        return True
    if isinstance(v, (int, float)):
        # Excel date typically >= 1
        return float(v) >= 1.0
    s = str(v)
    if DATE_RE.search(s):
        return True
    try:
        dt = pd.to_datetime(v, errors="coerce")
        if pd.isna(dt):
            return False
        # if original looks like time-only, avoid counting as date
        if isinstance(v, str) and TIME_RE.match(v.strip()):
            return False
        return True
    except Exception:
        return False


def _is_dow_like_value(v) -> bool:
    if pd.isna(v):
        return False
    s = _normalize_text(v)
    if s in {"2", "3", "4", "5", "6", "7", "8", "cn"}:
        return True
    if s.startswith("thu ") and s.split()[-1] in {"2", "3", "4", "5", "6", "7"}:
        return True
    if s in {"chu nhat", "chu nhat"}:
        return True
    return False


def _is_teacher_name_like(v) -> bool:
    if pd.isna(v):
        return False
    if isinstance(v, (int, float, datetime, time)):
        return False
    s = str(v).strip()
    if not s:
        return False
    if any(ch.isdigit() for ch in s):
        return False
    # must have at least two words
    if len(s.split()) < 2:
        return False
    # moderate length
    return 4 <= len(s) <= 60


def _is_class_code_like(v) -> bool:
    if pd.isna(v):
        return False
    s = str(v).strip()
    if not s:
        return False
    # common class code patterns
    if re.match(r"^[A-Za-z]{1,5}[-_]?\d{2,4}[A-Za-z0-9-]*$", s):
        return True
    if re.match(r"^[A-Za-z]{2,4}\d{2}[A-Za-z]?\d?$", s):
        return True
    return False


def _is_room_like(v) -> bool:
    if pd.isna(v):
        return False
    s = _normalize_text(v)
    raw = str(v)
    if any(prefix in s for prefix in ["phong", "p.", "p "]):
        return True
    if re.match(r"^[A-Za-z]{0,3}\.?\s?\d{2,4}[A-Za-z]?$", raw):
        return True
    return False


def _is_campus_like(v) -> bool:
    if pd.isna(v):
        return False
    s = _normalize_text(v)
    if "co so" in s:
        return True
    if re.match(r"^cs\s*\d+", s):
        return True
    return False


def _is_textual(v) -> bool:
    if pd.isna(v):
        return False
    if isinstance(v, (int, float, datetime, time)):
        return False
    s = str(v).strip()
    if not s:
        return False
    # exclude pure digits or time/date strings
    if s.isdigit() or TIME_RE.match(s) or DATE_RE.search(s):
        return False
    return True


def _ratio(series: pd.Series, pred, limit: int = 200) -> float:
    s = series.head(limit)
    total = 0
    hits = 0
    for v in s:
        if pd.isna(v):
            continue
        total += 1
        try:
            if pred(v):
                hits += 1
        except Exception:
            pass
    if total == 0:
        return 0.0
    return hits / total


def _auto_guess_columns(df: pd.DataFrame):
    ncols = df.shape[1]
    # Precompute features per column
    features = []
    norm_headers = [_normalize_text(c) for c in df.columns]
    for i in range(ncols):
        col = df.iloc[:, i]
        feats = {
            "date": _ratio(col, _is_date_like_value),
            "time": _ratio(col, _is_time_like_value),
            "dow": _ratio(col, _is_dow_like_value),
            "teacher": _ratio(col, _is_teacher_name_like),
            "class": _ratio(col, _is_class_code_like),
            "room": _ratio(col, _is_room_like),
            "campus": _ratio(col, _is_campus_like),
            "text": _ratio(col, _is_textual),
            "blank_ratio": col.isna().mean() if len(col) else 0.0,
        }
        feats["header"] = norm_headers[i]
        features.append(feats)

    used = set()

    def best_col(scores):
        # Return best index not used
        ordered = sorted(((sc, idx) for idx, sc in enumerate(scores)), reverse=True)
        for sc, idx in ordered:
            if idx not in used:
                used.add(idx)
                return idx
        return None

    # Build scores for each target
    scores_map = {}

    # Helper for header score by synonyms
    def header_score(i, keys):
        h = features[i]["header"]
        score = 0.0
        for k in keys:
            if k in h:
                score = max(score, 3.0)
            else:
                # fuzzy
                score = max(score, difflib.SequenceMatcher(None, k, h).ratio())
        return score

    # Ngày Bắt Đầu / Kết Thúc
    s_date_bd = []
    s_date_kt = []
    for i in range(ncols):
        f = features[i]
        s_bd = 4.0 * f["date"] + 1.5 * header_score(i, ["ngay bat dau", "ngay bd", "bat dau"]) - 1.0 * f["time"]
        s_kt = 4.0 * f["date"] + 1.5 * header_score(i, ["ngay ket thuc", "ngay kt", "ket thuc"]) - 1.0 * f["time"]
        s_date_bd.append(s_bd)
        s_date_kt.append(s_kt)
    idx_bd = best_col(s_date_bd)
    idx_kt = best_col(s_date_kt)

    # Thời Gian BĐ / KT
    s_time_bd = []
    s_time_kt = []
    for i in range(ncols):
        f = features[i]
        s_bd = 4.0 * f["time"] + 1.0 * header_score(i, ["thoi gian bd", "gio bd", "bat dau"]) - 0.5 * f["date"]
        s_kt = 4.0 * f["time"] + 1.0 * header_score(i, ["thoi gian kt", "gio kt", "ket thuc"]) - 0.5 * f["date"]
        s_time_bd.append(s_bd)
        s_time_kt.append(s_kt)
    idx_tbd = best_col(s_time_bd)
    idx_tkt = best_col(s_time_kt)

    # Thứ
    s_dow = [4.0 * features[i]["dow"] + 2.0 * header_score(i, ["thu"]) for i in range(ncols)]
    idx_dow = best_col(s_dow)

    # Phòng Học
    s_room = [3.0 * features[i]["room"] + 1.0 * header_score(i, ["phong"]) for i in range(ncols)]
    idx_room = best_col(s_room)

    # Cơ sở học
    s_campus = [3.0 * features[i]["campus"] + 1.0 * header_score(i, ["co so", "cs"]) for i in range(ncols)]
    idx_campus = best_col(s_campus)

    # Lớp
    s_class = [3.0 * features[i]["class"] + 1.0 * header_score(i, ["lop", "ma lop"]) for i in range(ncols)]
    idx_class = best_col(s_class)

    # Tên Giảng Viên
    s_teacher = [3.0 * features[i]["teacher"] + 1.5 * header_score(i, ["giang vien", "gv"]) for i in range(ncols)]
    idx_teacher = best_col(s_teacher)

    # Môn Học (ưu tiên text, không phải date/time, nhiều null vì merge)
    s_subject = []
    for i in range(ncols):
        f = features[i]
        score = 3.0 * f["text"] - 2.0 * f["date"] - 2.0 * f["time"] + 0.8 * header_score(i, ["mon", "mon hoc", "ten mon"]) + 0.5 * (1.0 if f["blank_ratio"] >= 0.3 else 0.0)
        s_subject.append(score)
    idx_subject = best_col(s_subject)

    # Khoa/Ngành Quản Lý (for filtering)
    s_nganh = []
    for i in range(ncols):
        f = features[i]
        # Strongly prefer header tokens matching "Khoa/Ngành Quản Lý"
        s = 4.0 * header_score(i, [
            "khoa nganh quan ly",
            "khoa nganh",
            "nganh quan ly",
            "khoa",
            "nganh",
        ]) + 0.5 * f["text"] + 0.2 * (1.0 if f["blank_ratio"] >= 0.2 else 0.0)
        s_nganh.append(s)
    idx_nganh = best_col(s_nganh)

    mapping_pos = {
        "Môn Học": (idx_subject + 1) if idx_subject is not None else 0,
        "Lớp": (idx_class + 1) if idx_class is not None else 0,
        "Tên Giảng Viên": (idx_teacher + 1) if idx_teacher is not None else 0,
        "Ngày Bắt Đầu": (idx_bd + 1) if idx_bd is not None else 0,
        "Ngày Kết Thúc": (idx_kt + 1) if idx_kt is not None else 0,
        "Thời Gian BĐ": (idx_tbd + 1) if idx_tbd is not None else 0,
        "Thời gian KT": (idx_tkt + 1) if idx_tkt is not None else 0,
        "Thứ": (idx_dow + 1) if idx_dow is not None else 0,
        "Phòng Học": (idx_room + 1) if idx_room is not None else 0,
        "Cơ sở học": (idx_campus + 1) if idx_campus is not None else 0,
        "Ngành": (idx_nganh + 1) if idx_nganh is not None else 0,
    }
    return mapping_pos


# -----------------------------
# Streamlit App
# -----------------------------
st.set_page_config(page_title="Đọc TKB HK2 (Mới Giảng)", layout="wide")
st.title("Đọc file TKB HK2 - Mới Giảng")
st.caption(
    "Đọc sheet 'TKB_HK2_Moi_Giang' từ file 'file_data.xlsx', lấy các cột yêu cầu."
)

with st.sidebar:
    st.header("Cấu hình")
    default_path = "file_data.xlsx"
    sheet_name = st.text_input("Tên sheet", value="TKB_HK2_Moi_Giang")
    header_row_excel = st.number_input(
        "Hàng header (Excel)", min_value=1, value=2, step=1,
        help="Ví dụ: header ở hàng 2 => nhập 2",
        key="header_row_excel",
    )
    data_start_row_excel = st.number_input(
        "Bắt đầu dữ liệu từ hàng (Excel)",
        min_value=1,
        value=int(header_row_excel) + 1,
        step=1,
        help="Thường là hàng ngay sau header",
        key="data_start_row_excel",
    )
    uploaded = st.file_uploader("Tải lên file Excel (tùy chọn)", type=["xlsx"])
    method = st.radio(
        "Chọn cách lấy cột",
        options=["Theo số cột", "Theo tiêu đề"],
        index=0,
        help="Nếu tiêu đề không chuẩn, hãy dùng 'Theo số cột' và nhập số cột (1-based).",
    )
    merge_classes_one_row = st.checkbox(
        "Gộp nhiều lớp (GV + Môn) nếu cùng Ngày Bắt Đầu/Kết Thúc",
        value=True,
        help="Nếu một giảng viên dạy cùng một môn cho nhiều lớp trong cùng khoảng thời gian (Ngày BĐ/Kết Thúc), sẽ gộp thành 1 dòng (gộp Lớp/Thứ/Giờ/Phòng/Cơ sở).",
    )


def load_dataframe():
    try:
        if uploaded is not None:
            df = pd.read_excel(uploaded, sheet_name=sheet_name, header=int(header_row_excel) - 1)
            source_label = f"Đọc từ file tải lên: {uploaded.name}"
        else:
            df = pd.read_excel(default_path, sheet_name=sheet_name, header=int(header_row_excel) - 1)
            source_label = f"Đọc từ '{default_path}'"
    except FileNotFoundError:
        st.error(f"Không tìm thấy file: {default_path}. Vui lòng tải lên file Excel.")
        return None, None
    except ValueError as e:
        st.error(f"Lỗi đọc sheet: {e}")
        return None, None
    except Exception as e:
        st.error(f"Lỗi khi đọc Excel: {e}")
        return None, None

    # Cắt dữ liệu từ hàng dữ liệu N (Excel)
    # Sau khi đọc với header=H-1, dòng dữ liệu đầu tiên tương ứng Excel (H+1) sẽ là index=0
    # Vì vậy, offset = N - (H + 1)
    start_idx = max(0, int(data_start_row_excel) - (int(header_row_excel) + 1))
    if start_idx > 0:
        df = df.iloc[start_idx:].reset_index(drop=True)

    return df, source_label


df_raw, source_note = load_dataframe()

if df_raw is not None:
    st.success(source_note)

    expected = list(TARGET_DISPLAY.values())

    if method == "Theo số cột":
        # Preview header and example values to help user set indices
        st.subheader("Xem nhanh tiêu đề và ví dụ")
        header_preview = pd.DataFrame(
            {
                "Cột # (1-based)": list(range(1, df_raw.shape[1] + 1)),
                "Tiêu đề": [str(c) for c in df_raw.columns],
                "Ví dụ hàng 1": [
                    df_raw.iloc[0, i] if len(df_raw) > 0 else "" for i in range(df_raw.shape[1])
                ],
            }
        )
        st.dataframe(header_preview, use_container_width=True, height=280)

        st.subheader("Chọn cột cho từng trường (dựa vào bảng xem nhanh)")
        # Auto guess columns
        guessed = _auto_guess_columns(df_raw)
        st.info("Đã tự động đề xuất cột. Bạn có thể điều chỉnh nếu cần.")
        # Build select options from preview: list of (pos,label)
        options = [(0, "0 — (Bỏ qua)")]
        for i in range(df_raw.shape[1]):
            col = str(df_raw.columns[i])
            sample = ""
            if len(df_raw) > 0:
                val = df_raw.iloc[0, i]
                sample = str(val) if not pd.isna(val) else ""
            # Truncate sample for readability
            if len(sample) > 60:
                sample = sample[:57] + "..."
            label = f"{i+1} — {col} — {sample}" if sample else f"{i+1} — {col}"
            options.append((i + 1, label))

        labels = [lbl for _, lbl in options]
        label_to_pos = {lbl: pos for pos, lbl in options}
        pos_to_label = {pos: lbl for pos, lbl in options}

        cols = st.columns(2)
        mapping_idx = {}
        for idx, disp in enumerate(expected):
            with cols[idx % 2]:
                # default selection based on auto-guess
                default_pos = int(guessed.get(disp, 0) or 0)
                default_label = pos_to_label.get(default_pos, labels[0])
                try:
                    default_index = labels.index(default_label)
                except ValueError:
                    default_index = 0

                chosen_label = st.selectbox(
                    f"{disp}",
                    options=labels,
                    index=default_index,
                    key=f"col_sel_{idx}",
                    help="Chọn theo số cột từ bảng xem nhanh bên trên",
                )
                mapping_idx[disp] = label_to_pos.get(chosen_label, 0)

        # Select Ngành column for filtering and display name column
        st.subheader("Chọn cột 'Khoa/Ngành Quản Lý' để lọc và hiển thị tên")
        default_nganh_pos = int(guessed.get("Ngành", 0) or 0)
        default_nganh_label = pos_to_label.get(default_nganh_pos, labels[0])
        try:
            default_nganh_index = labels.index(default_nganh_label)
        except ValueError:
            default_nganh_index = 0
        chosen_label_nganh = st.selectbox(
            "Khoa/Ngành Quản Lý (khóa lọc)",
            options=labels,
            index=default_nganh_index,
            key="col_sel_nganh",
            help="Cột dùng làm khóa lọc (thường là 'Khoa/Ngành Quản Lý')",
        )
        nganh_pos = label_to_pos.get(chosen_label_nganh, 0)

        # Tên Ngành (nhãn hiển thị) — mặc định cùng cột với Ngành
        try:
            default_nganh_name_index = default_nganh_index
        except Exception:
            default_nganh_name_index = 0
        chosen_label_nganh_name = st.selectbox(
            "Tên Khoa/Ngành (hiển thị)",
            options=labels,
            index=default_nganh_name_index,
            key="col_sel_nganh_name",
            help="Cột hiển thị tên ngành trong bộ lọc (nếu khác)",
        )
        nganh_name_pos = label_to_pos.get(chosen_label_nganh_name, 0)

        # Build selected DataFrame from indices (ensure all expected columns present and ordered)
        data = {}
        for disp in expected:
            pos = int(mapping_idx.get(disp, 0) or 0)
            if pos and 1 <= pos <= df_raw.shape[1]:
                data[disp] = df_raw.iloc[:, pos - 1]
            else:
                data[disp] = pd.Series([None] * len(df_raw))

        df_sel = pd.DataFrame(data)[expected]

        # Apply Ngành filter if selected
        if nganh_pos and 1 <= int(nganh_pos) <= df_raw.shape[1]:
            key_series = df_raw.iloc[:, int(nganh_pos) - 1].ffill()
            if nganh_name_pos and 1 <= int(nganh_name_pos) <= df_raw.shape[1]:
                label_series = df_raw.iloc[:, int(nganh_name_pos) - 1].ffill()
            else:
                label_series = key_series

            key_series = key_series.astype(str).map(lambda s: s.strip())
            label_series = label_series.astype(str).map(lambda s: s.strip())

            # Build label -> set(keys) mapping (many-to-one safe)
            mapping = {}
            for k, lbl in zip(key_series, label_series):
                if not k:
                    continue
                if not lbl:
                    lbl = k
                mapping.setdefault(lbl, set()).add(k)

            labels_nganh = sorted(mapping.keys(), key=lambda x: x.lower())

            with st.sidebar:
                st.subheader("Bộ lọc Khoa/Ngành Quản Lý")
                selected_labels = st.multiselect(
                    "Chọn Khoa/Ngành (hiển thị theo tên)",
                    options=labels_nganh,
                    default=labels_nganh,
                    help="Bỏ chọn để lọc ra các ngành mong muốn",
                )

            if selected_labels and len(selected_labels) != len(labels_nganh):
                allowed_keys = set()
                for lbl in selected_labels:
                    allowed_keys.update(mapping.get(lbl, set()))
                mask = key_series.isin(allowed_keys)
                df_sel = df_sel[mask].reset_index(drop=True)
    else:
        # Theo tiêu đề (mặc định trước đây)
        col_map = _find_column_mapping(df_raw.columns)
        missing = [c for c in expected if c not in col_map]
        if missing:
            st.warning(
                "Không tìm thấy đủ cột yêu cầu: "
                + ", ".join(missing)
                + ".\nVẫn sẽ hiển thị các cột tìm thấy."
            )
        # Prepare data with blanks for missing columns, then fill mapped ones
        data = {disp: pd.Series([None] * len(df_raw)) for disp in expected}
        for disp, src in col_map.items():
            # disp are display names present in mapping
            if disp in data:
                data[disp] = df_raw[src]

        df_sel = pd.DataFrame(data)[expected]

    # Xử lý merge: Môn Học -> forward fill
    if "Môn Học" in df_sel.columns:
        df_sel["Môn Học"] = df_sel["Môn Học"].ffill()

    # Chuẩn hóa ngày
    for dcol in ["Ngày Bắt Đầu", "Ngày Kết Thúc"]:
        if dcol in df_sel.columns:
            df_sel[dcol] = df_sel[dcol].apply(_fmt_date)

    # Chuẩn hóa thời gian
    for tcol in ["Thời Gian BĐ", "Thời gian KT"]:
        if tcol in df_sel.columns:
            df_sel[tcol] = df_sel[tcol].apply(_excel_time_to_time).apply(_fmt_time)

    # Gộp nhiều lớp theo (GV + Môn) và cùng khoảng thời gian (Ngày BĐ/Kết Thúc)
    df_out = df_sel.copy()
    if merge_classes_one_row and all(c in df_out.columns for c in expected):
        # Chỉ khóa theo 4 trường này; các trường khác sẽ gộp giá trị duy nhất
        group_keys = ["Môn Học", "Tên Giảng Viên", "Ngày Bắt Đầu", "Ngày Kết Thúc"]

        def join_unique(series: pd.Series) -> str:
            vals = []
            for v in series:
                if pd.isna(v):
                    continue
                s = str(v).strip()
                if not s:
                    continue
                if s not in vals:
                    vals.append(s)
            return ", ".join(vals)

        tmp = df_out.copy()
        for k in group_keys:
            if k in tmp.columns:
                tmp[k] = tmp[k].fillna("")

        agg_map = {"Lớp": join_unique}
        for c in ["Thời Gian BĐ", "Thời gian KT", "Thứ", "Phòng Học", "Cơ sở học"]:
            if c in tmp.columns:
                agg_map[c] = join_unique

        try:
            df_out = (
                tmp.groupby(group_keys, as_index=False)
                .agg(agg_map)
                .replace({"": None})
            )
        except Exception:
            df_out = (
                df_out.groupby(group_keys, as_index=False)
                .agg(agg_map)
            )

        # Đảm bảo đúng thứ tự cột
        for col in expected:
            if col not in df_out.columns:
                df_out[col] = None
        df_out = df_out[expected]

    st.subheader("Dữ liệu đã trích xuất")
    st.dataframe(df_out, use_container_width=True)

    # Xuất dữ liệu
    st.subheader("Tải xuống")
    csv_data = df_out.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "Tải về CSV",
        data=csv_data,
        file_name="tkb_hk2_moi_giang.csv",
        mime="text/csv",
    )

    # Excel download
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df_out.to_excel(writer, index=False, sheet_name="TKB")
    st.download_button(
        "Tải về Excel",
        data=buffer.getvalue(),
        file_name="tkb_hk2_moi_giang.xlsx",
        mime=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
else:
    st.info("Hãy chọn/tải file và cấu hình ở thanh bên nếu cần.")
