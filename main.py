import io
from datetime import datetime, time, timedelta
import unicodedata
import difflib
import re
import os

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


def _get_secret(key: str, default=None):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


# Render only placeholders like {name} (letters/digits/underscore).
# Ignores CSS braces like body { ... }.
_PH_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _render_tpl(template: str, mapping: dict) -> str:
    tpl = template or ""
    def _repl(m: re.Match):
        key = m.group(1)
        val = mapping.get(key)
        return "" if val is None else str(val)
    try:
        return _PH_RE.sub(_repl, tpl)
    except Exception:
        return tpl


# -----------------------------
# Gmail credential persistence
# -----------------------------
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
GMAIL_TOKEN_FILE = os.path.join(".streamlit", "gmail_token.json")


def _save_gmail_creds_to_file(creds) -> bool:
    try:
        os.makedirs(os.path.dirname(GMAIL_TOKEN_FILE), exist_ok=True)
        # creds.to_json() returns a JSON string
        with open(GMAIL_TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
        return True
    except Exception:
        return False


def _clear_gmail_creds_file() -> bool:
    try:
        if os.path.exists(GMAIL_TOKEN_FILE):
            os.remove(GMAIL_TOKEN_FILE)
        return True
    except Exception:
        return False


def _load_gmail_creds_from_file():
    try:
        if not os.path.exists(GMAIL_TOKEN_FILE):
            return None
        import json
        from google.oauth2.credentials import Credentials
        with open(GMAIL_TOKEN_FILE, "r", encoding="utf-8") as f:
            data = f.read()
        info = json.loads(data)
        creds = Credentials.from_authorized_user_info(info, scopes=GMAIL_SCOPES)
        # Refresh if needed
        if creds and creds.expired and creds.refresh_token:
            try:
                from google.auth.transport.requests import Request
                creds.refresh(Request())
                _save_gmail_creds_to_file(creds)
            except Exception:
                # If refresh fails, treat as missing
                return None
        return creds
    except Exception:
        return None


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
        "mon",
        "hoc phan",
        "ten hoc phan",
        "mon hoc phan",
        "mon hp",
        "ten hp",
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

# Extend targets and synonyms for Email
if "email" not in TARGET_DISPLAY:
    TARGET_DISPLAY["email"] = "Email"
SYNONYMS.setdefault("email", {"email", "e mail", "mail", "gmail", "thu dien tu"})


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


def _is_email_like_value(v) -> bool:
    if pd.isna(v):
        return False
    if isinstance(v, (datetime, time)):
        return False
    s = str(v).strip()
    if not s:
        return False
    return re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s) is not None


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
            "email": _ratio(col, _is_email_like_value),
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

    # Email
    s_email = [4.0 * features[i]["email"] + 1.5 * header_score(i, ["email", "e mail", "mail", "gmail"]) for i in range(ncols)]
    idx_email = best_col(s_email)

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
        "Email": (idx_email + 1) if idx_email is not None else 0,
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
                "Cột # (1-based)": [str(x) for x in list(range(1, df_raw.shape[1] + 1))],
                "Tiêu đề": [str(c) for c in df_raw.columns],
                "Ví dụ hàng 1": [
                    str(df_raw.iloc[0, i]) if len(df_raw) > 0 else "" for i in range(df_raw.shape[1])
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

    # Detect if teacher column was merged (blank rows under a header value)
    teacher_was_merged = False
    try:
        teacher_label = TARGET_DISPLAY.get("ten giang vien")
        if teacher_label in df_sel.columns:
            s0 = df_sel[teacher_label]
            teacher_was_merged = s0.isna().sum() > 0 and s0.notna().any()
    except Exception:
        teacher_was_merged = False

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
    # Detect if teacher column was merged (blank rows) before ffill
    teacher_was_merged = False
    try:
        teacher_label = TARGET_DISPLAY.get("ten giang vien")
        if teacher_label in df_sel.columns:
            s0 = df_sel[teacher_label]
            teacher_was_merged = s0.isna().sum() > 0 and s0.notna().any()
    except Exception:
        teacher_was_merged = False


    # Xử lý merge: forward fill cho các cột bị merge dòng
    if "Môn Học" in df_sel.columns:
        df_sel["Môn Học"] = df_sel["Môn Học"].ffill()
    if "Tên Giảng Viên" in df_sel.columns:
        df_sel["Tên Giảng Viên"] = df_sel["Tên Giảng Viên"].ffill()
    if "Email" in df_sel.columns:
        df_sel["Email"] = df_sel["Email"].ffill()

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
        # Override: always group by GV + Môn only (ignore date range)
        group_keys = ["Môn Học", "Tên Giảng Viên"]

        # Group key resolver prefers display label, then synonyms/fuzzy
        def _find_col_key(df, target_key: str):
            disp = TARGET_DISPLAY.get(target_key)
            if disp and disp in df.columns:
                return disp
            norm_map = {_normalize_text(c): c for c in df.columns}
            candidates = set([target_key]) | set(SYNONYMS.get(target_key, {target_key}))
            for k in candidates:
                if k in norm_map:
                    return norm_map[k]
            for k in candidates:
                words = [w for w in k.split() if w]
                for norm_col, orig_col in norm_map.items():
                    hay = f" {norm_col} "
                    if all(f" {w} " in hay for w in words):
                        return orig_col
            best = None
            best_score = 0.0
            for norm_col, orig_col in norm_map.items():
                for k in candidates:
                    score = difflib.SequenceMatcher(None, k, norm_col).ratio()
                    if score > best_score:
                        best_score = score
                        best = orig_col
            return best if best_score >= 0.6 else None

        # Choose grouping behavior
        # Always group by Teacher and Email when merge_classes_one_row is checked
        group_keys = [
            _find_col_key(df_out, "ten giang vien"),
            _find_col_key(df_out, "email"),
        ]
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

        # Ensure Email retained during aggregation
        if "Email" in tmp.columns:
            agg_map["Email"] = join_unique

        # Gộp tất cả các cột không thuộc group_keys bằng cách nối giá trị duy nhất
        agg_map = {c: join_unique for c in tmp.columns if c not in group_keys}

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

    # --- Xem theo giang vien: dropdown + bang mon hoc, tach hang theo ngay ---
    def _find_col_simple(df, target_key: str):
        disp = TARGET_DISPLAY.get(target_key)
        if disp and disp in df.columns:
            return disp
        norm_map = {_normalize_text(c): c for c in df.columns}
        candidates = set([target_key]) | set(SYNONYMS.get(target_key, {target_key}))
        def _excluded_for_subject(norm_name: str) -> bool:
            if target_key != "mon hoc":
                return False
            bad_tokens = ["lop", "ma lop", "nhom", "nhom lop"]
            return any(bt in norm_name for bt in bad_tokens)
        # 1) exact normalized match
        for k in candidates:
            if k in norm_map and not _excluded_for_subject(k):
                return norm_map[k]
        # 2) containment by whole words
        for k in candidates:
            words = [w for w in k.split() if w]
            for norm_col, orig_col in norm_map.items():
                hay = f" {norm_col} "
                if all(f" {w} " in hay for w in words) and not _excluded_for_subject(norm_col):
                    return orig_col
        # 3) fuzzy
        best = None
        best_score = 0.0
        for norm_col, orig_col in norm_map.items():
            for k in candidates:
                score = difflib.SequenceMatcher(None, k, norm_col).ratio()
                if score > best_score and not _excluded_for_subject(norm_col):
                    best_score = score
                    best = orig_col
        if best_score >= 0.6:
            return best
        return None

    st.subheader("Xem theo giang vien")
    # Use pre-merge dataframe for accurate per-subject rows
    try:
        df_view_src = df_sel.copy()
    except Exception:
        df_view_src = df_out.copy()
    col_teacher = _find_col_simple(df_view_src, "ten giang vien")
    col_subject = _find_col_simple(df_view_src, "mon hoc")
    col_class = _find_col_simple(df_view_src, "lop")
    col_bd = _find_col_simple(df_view_src, "ngay bat dau")
    col_kt = _find_col_simple(df_view_src, "ngay ket thuc")
    col_coso = _find_col_simple(df_view_src, "co so hoc")
    col_room = _find_col_simple(df_view_src, "phong hoc")

    if col_teacher and col_subject:
        try:
            teacher_names = [str(x) for x in df_view_src[col_teacher].dropna().unique().tolist() if str(x).strip()]
            teacher_names = sorted(teacher_names, key=lambda s: s.lower())
        except Exception:
            teacher_names = []

        if teacher_names:
            selected_teacher = st.selectbox("Chon giang vien", options=teacher_names, key="gv_dropdown")

            if selected_teacher:
                df_gv = df_view_src[df_view_src[col_teacher] == selected_teacher].copy()

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

                group_keys = []
                if col_subject:
                    group_keys.append(col_subject)

                agg_map = {}
                if col_class:
                    agg_map[col_class] = _join_unique
                if col_coso:
                    agg_map[col_coso] = _join_unique
                if col_room:
                    agg_map[col_room] = _join_unique
                if col_bd:
                    agg_map[col_bd] = _join_unique
                if col_kt:
                    agg_map[col_kt] = _join_unique

                try:
                    df_sum = df_gv.groupby(group_keys, as_index=False).agg(agg_map) if agg_map else df_gv[group_keys].drop_duplicates()
                except Exception:
                    df_sum = df_gv[group_keys + list(agg_map.keys())]

                # Rename columns for display
                rename_map = {}
                if col_subject:
                    rename_map[col_subject] = "Mon hoc"
                if col_class:
                    rename_map[col_class] = "Lop"
                if col_bd:
                    rename_map[col_bd] = "Ngay bat dau"
                if col_kt:
                    rename_map[col_kt] = "Ngay ket thuc"
                if col_coso:
                    rename_map[col_coso] = "Co so"
                if col_room:
                    rename_map[col_room] = "Phong hoc"
                df_sum = df_sum.rename(columns=rename_map)

                order_cols = [c for c in ["Mon hoc", "Lop", "Ngay bat dau", "Ngay ket thuc", "Co so", "Phong hoc"] if c in df_sum.columns]
                df_sum = df_sum[order_cols]

                st.dataframe(df_sum, use_container_width=True)
        else:
            st.info("Khong tim thay danh sach giang vien")
    else:
        st.info("Khong tim thay cot giang vien/mon hoc")

    # --- Gửi email: chọn người nhận bằng checkbox và gửi ---
    st.subheader("Gửi email")
    group_send_by_teacher = st.checkbox("Gui gop theo giang vien", value=True, key="group_send_gv")

    # Helper: robustly find column by target key using TARGET_DISPLAY label first, then synonyms + fuzzy
    def _find_col(df, target_key: str):
        disp = TARGET_DISPLAY.get(target_key)
        if disp and disp in df.columns:
            return disp
        norm_map = {_normalize_text(c): c for c in df.columns}
        candidates = set([target_key]) | set(SYNONYMS.get(target_key, {target_key}))
        # 1) exact normalized match
        for k in candidates:
            if k in norm_map:
                return norm_map[k]
        # 2) containment by whole words
        for k in candidates:
            words = [w for w in k.split() if w]
            for norm_col, orig_col in norm_map.items():
                hay = f" {norm_col} "
                if all(f" {w} " in hay for w in words):
                    return orig_col
        # 3) fuzzy
        best = None
        best_score = 0.0
        for norm_col, orig_col in norm_map.items():
            for k in candidates:
                score = difflib.SequenceMatcher(None, k, norm_col).ratio()
                if score > best_score:
                    best_score = score
                    best = orig_col
        if best_score >= 0.6:
            return best
        return None

    col_teacher = _find_col(df_out, "ten giang vien")
    col_email = _find_col(df_out, "email")
    col_subject = _find_col(df_out, "mon hoc")
    col_class = _find_col(df_out, "lop")

    # Prepare compact view for selection
    def _fmt(v):
        if pd.isna(v):
            return ""
        s = str(v)
        return s

    mail_view = pd.DataFrame({
        "Tên Giảng Viên": df_out[col_teacher] if col_teacher else pd.Series([None]*len(df_out)),
        "Email": df_out[col_email] if col_email else pd.Series([None]*len(df_out)),
        "Môn Học": df_out[col_subject] if col_subject else pd.Series([None]*len(df_out)),
        "Lớp": df_out[col_class] if col_class else pd.Series([None]*len(df_out)),
    }) if len(df_out) else pd.DataFrame(columns=["Tên Giảng Viên","Email","Môn Học","Lớp"]) 

    # Action buttons for selection
    sel_all_col, clear_all_col, send_col = st.columns([1,1,2])
    if sel_all_col.button("Chọn tất cả"):
        # mark all as selected in session state
        for i in range(len(mail_view)):
            st.session_state[f"mail_sel_{i}"] = True
    if clear_all_col.button("Bỏ chọn tất cả"):
        for i in range(len(mail_view)):
            st.session_state[f"mail_sel_{i}"] = False

    # Render per-row checkboxes
    selected_indices = []
    for i in range(len(mail_view)):
        row = mail_view.iloc[i]
        label = f"{_fmt(row.get('Tên Giảng Viên'))} | {_fmt(row.get('Email'))} | {_fmt(row.get('Môn Học'))} | {_fmt(row.get('Lớp'))}"
        if st.checkbox(label, key=f"mail_sel_{i}"):
            selected_indices.append(i)

    # Gmail OAuth (optional)
    st.caption("Tùy chọn: dùng Gmail API (OAuth) để gửi mail")
    gmail_col1, gmail_col2 = st.columns([1,1])
    use_gmail_api = st.checkbox("Su dung Gmail (OAuth)", value=True, key="use_gmail_api")
    # Auto-load saved Gmail credentials once per session
    if use_gmail_api and "gmail_creds" not in st.session_state:
        try:
            creds_loaded = _load_gmail_creds_from_file()
            if creds_loaded:
                st.session_state["gmail_creds"] = creds_loaded.to_json()
                try:
                    from googleapiclient.discovery import build
                    service = build("gmail", "v1", credentials=creds_loaded)
                    profile = service.users().getProfile(userId="me").execute()
                    st.session_state["gmail_addr"] = profile.get("emailAddress")
                except Exception:
                    pass
                st.caption("Loaded saved Gmail connection")
        except Exception:
            pass
    gmail_client_id = st.text_input(
        "Gmail Client ID",
        value=_get_secret("GOOGLE_CLIENT_ID", ""),
        help="Đặt trong .streamlit/secrets.toml hoặc nhập trực tiếp",
    )
    gmail_client_secret = st.text_input(
        "Gmail Client Secret",
        type="password",
        value=_get_secret("GOOGLE_CLIENT_SECRET", ""),
        help="Đặt trong .streamlit/secrets.toml hoặc nhập trực tiếp",
    )
    if use_gmail_api and gmail_col1.button("Kết nối Gmail"):
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            scopes = ["https://www.googleapis.com/auth/gmail.send"]
            client_config = {
                "installed": {
                    "client_id": gmail_client_id,
                    "client_secret": gmail_client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
                }
            }
            flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes)
            creds = flow.run_local_server(port=0, prompt="consent")
            st.session_state["gmail_creds"] = creds.to_json()
            # Persist for next runs
            try:
                _save_gmail_creds_to_file(creds)
            except Exception:
                pass
            try:
                service = build("gmail", "v1", credentials=creds)
                profile = service.users().getProfile(userId="me").execute()
                st.session_state["gmail_addr"] = profile.get("emailAddress")
            except Exception:
                pass
            st.success("Đã kết nối Gmail")
        except ModuleNotFoundError:
            st.error("Thiếu thư viện: pip install google-api-python-client google-auth google-auth-oauthlib")
        except Exception as e:
            st.error(f"Lỗi kết nối Gmail: {e}")
    if use_gmail_api and gmail_col2.button("Ngắt kết nối"):
        st.session_state.pop("gmail_creds", None)
        st.session_state.pop("gmail_addr", None)
        try:
            _clear_gmail_creds_file()
            st.info("Cleared saved Gmail connection")
        except Exception:
            pass
    if st.session_state.get("gmail_addr"):
        st.caption(f"Đang dùng: {st.session_state.get('gmail_addr')}")

    # SMTP config in sidebar
    with st.sidebar:
        st.subheader("Gửi mail - Cấu hình")
        mail_subject = st.text_input("Tieu de", value=_get_secret("MAIL_SUBJECT", "Thong bao lich giang"))
        mail_body = st.text_area(
            "Noi dung (co the dung {ten_gv}, {mon_hoc}, {lop})",
            value=_get_secret("MAIL_BODY", "Kinh gui {ten_gv},\n\nThong tin lich giang cua Thay/Co:\n{lich_text}\n\nTran trong."),
            height=120,
        )



        # HTML email template (optional)
        mail_use_html = st.checkbox("Gui dang HTML", value=True, key="mail_use_html")
        default_html = ""
        try:
            with open(os.path.join("templates", "invite_email.html"), "r", encoding="utf-8") as f:
                default_html = f.read()
        except Exception:
            default_html = (
                "<html><body><p>Kinh gui {ten_gv},</p>"
                "<p>Khoa Cong nghe – Truong Cao dang Viet My tran trong moi Thay/Co"
                " tham gia giang day theo thong tin sau:</p>"
                "{lich_html}"
                "<p>Kinh mong Thay/Co sap xep thoi gian tham gia giang day theo ke hoach tren."
                " Moi thong tin chi tiet, xin vui long lien he Khoa Cong nghe de duoc ho tro.</p>"
                "</body></html>"
            )
        mail_body_html = None
        if mail_use_html:
            mail_body_html = st.text_area(
                "Noi dung HTML (template)", value=default_html, height=260, key="mail_body_html"
            )

    # Email sending logic
    # Resolve optional column names for template fields
    col_bd = _find_col(df_out, "ngay bat dau")
    col_kt = _find_col(df_out, "ngay ket thuc")
    col_tgbd = _find_col(df_out, "thoi gian bd")
    col_tgkt = _find_col(df_out, "thoi gian kt")
    col_thu2 = _find_col(df_out, "thu")
    col_room2 = _find_col(df_out, "phong hoc")
    col_coso2 = _find_col(df_out, "co so hoc")


    def _build_varmap(i: int) -> dict:
        src = df_out.iloc[i]
        def g(c):
            return _fmt(src.get(c)) if c else ""
        now = datetime.now()
        return {
            "ten_gv": g(col_teacher),
            "mon_hoc": g(col_subject),
            "lop": g(col_class),
            "ngay_bd": g(col_bd),
            "ngay_kt": g(col_kt),
            "tg_bd": g(col_tgbd),
            "tg_kt": g(col_tgkt),
            "thu": g(col_thu2),
            "phong": g(col_room2),
            "co_so": g(col_coso2),
            # optional placeholders used by HTML template
            "ngay_gui": now.strftime("%d/%m/%Y"),
            "nam": now.strftime("%Y"),
            "logo_url": _get_secret("LOGO_URL", ""),
            "confirm_link": _get_secret("CONFIRM_LINK", ""),
        }
    # Preview (show first selected row with formatted subject/body)
    st.subheader("Xem truoc")
    if selected_indices:
        idx0 = selected_indices[0]
        src = df_out.iloc[idx0]
        varmap_prev = {
            "ten_gv": _fmt(src.get(col_teacher)) if col_teacher else "",
            "mon_hoc": _fmt(src.get(col_subject)) if col_subject else "",
            "lop": _fmt(src.get(col_class)) if col_class else "",
            "ngay_bd": _fmt(src.get(col_bd)) if col_bd else "",
            "ngay_kt": _fmt(src.get(col_kt)) if col_kt else "",
            "tg_bd": _fmt(src.get(col_tgbd)) if col_tgbd else "",
            "tg_kt": _fmt(src.get(col_tgkt)) if col_tgkt else "",
            "thu": _fmt(src.get(col_thu2)) if col_thu2 else "",
            "phong": _fmt(src.get(col_room2)) if col_room2 else "",
            "co_so": _fmt(src.get(col_coso2)) if col_coso2 else "",
        }
        # Add lich_html to varmap_prev for preview
        txt_table_prev, html_table_prev = _make_tables([idx0]) # Make table for single row
        varmap_prev["lich_text"] = txt_table_prev
        varmap_prev["lich_html"] = html_table_prev

        subj_prev = _render_tpl(mail_subject, varmap_prev)
        text_prev = _render_tpl(mail_body, varmap_prev)
        html_prev = _render_tpl(mail_body_html, varmap_prev) if 'mail_body_html' in locals() and mail_body_html else None
        st.text(f"Tieu de: {subj_prev}")
        st.write("Noi dung (text):")
        st.code(text_prev or "", language="markdown")
        if html_prev:
            st.write("Noi dung (HTML):")
            st.markdown(html_prev, unsafe_allow_html=True)
            with st.expander("Xem HTML thô"):
                st.code(html_prev, language="html")
    else:
        st.info("Chon it nhat 1 nguoi nhan de xem preview")
    def _send_email(to_addr: str, subject: str, body: str, html: str = None) -> tuple[bool, str]:
        try:
            from email.message import EmailMessage
            import smtplib
            msg = EmailMessage()
            msg["From"] = mail_from
            msg["To"] = to_addr
            msg["Subject"] = subject
            msg.set_content(body or "")
            if html:
                msg.add_alternative(html, subtype="html")

            server = smtplib.SMTP(smtp_host, int(smtp_port))
            server.starttls()
            if smtp_user:
                server.login(smtp_user, smtp_pass)
            server.send_message(msg)
            server.quit()
            return True, ""
        except Exception as e:
            return False, str(e)

    def _send_email_gmail(to_addr: str, subject: str, body: str, html: str = None) -> tuple[bool, str]:
        try:
            import json, base64
            from email.message import EmailMessage
            from googleapiclient.discovery import build
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request

            info = st.session_state.get("gmail_creds")
            if not info:
                return False, "Chua ket noi Gmail"

            if isinstance(info, str):
                info_dict = json.loads(info)
            elif isinstance(info, dict):
                info_dict = info
            else:
                return False, "Thong tin Gmail khong hop le"

            creds = Credentials.from_authorized_user_info(info_dict, scopes=GMAIL_SCOPES)
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    st.session_state["gmail_creds"] = creds.to_json()
                    _save_gmail_creds_to_file(creds)
                except Exception:
                    pass

            msg = EmailMessage()
            from_addr = st.session_state.get("gmail_addr") or _get_secret("MAIL_FROM", "")
            if from_addr:
                msg["From"] = from_addr
            msg["To"] = to_addr
            msg["Subject"] = subject or ""
            msg.set_content(body or "")
            if html:
                msg.add_alternative(html, subtype="html")

            service = build("gmail", "v1", credentials=creds)
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            service.users().messages().send(userId="me", body={"raw": raw}).execute()
            return True, ""
        except Exception as e:
            return False, str(e)

    # Table builder
    def _make_tables(idxs):
        cols = {
            "mon": _find_col(df_out, "mon hoc"),
            "lop": _find_col(df_out, "lop"),
            "thu": _find_col(df_out, "thu"),
            "tgbd": _find_col(df_out, "thoi gian bd"),
            "tgkt": _find_col(df_out, "thoi gian kt"),
            "nbd": _find_col(df_out, "ngay bat dau"),
            "nkt": _find_col(df_out, "ngay ket thuc"),
            "phong": _find_col(df_out, "phong hoc"),
            "coso": _find_col(df_out, "co so hoc"),
        }
        lines = ["Mon | Lop | Thu | Gio | Ngay | Phong | Co so"]
        header_html = "<tr><th>Môn</th><th>Lớp</th><th>Thứ</th><th>Giờ</th><th>Ngày</th><th>Phòng</th><th>Cơ sở</th></tr>"
        rows_html = []
        for i in idxs:
            s = df_out.iloc[i]
            mon = _fmt(s.get(cols["mon"])) if cols["mon"] else ""
            lop = _fmt(s.get(cols["lop"])) if cols["lop"] else ""
            thu = _fmt(s.get(cols["thu"])) if cols["thu"] else ""
            gio = "{}-{}".format(_fmt(s.get(cols["tgbd"])) if cols["tgbd"] else "", _fmt(s.get(cols["tgkt"])) if cols["tgkt"] else "").strip("-")
            ngay = "{}→{}".format(_fmt(s.get(cols["nbd"])) if cols["nbd"] else "", _fmt(s.get(cols["nkt"])) if cols["nkt"] else "").strip("→")
            phong = _fmt(s.get(cols["phong"])) if cols["phong"] else ""
            coso = _fmt(s.get(cols["coso"])) if cols["coso"] else ""
            lines.append(" | ".join([mon, lop, thu, gio, ngay, phong, coso]))
            rows_html.append(f"<tr><td>{mon}</td><td>{lop}</td><td>{thu}</td><td>{gio}</td><td>{ngay}</td><td>{phong}</td><td>{coso}</td></tr>")
        return "\n".join(lines), f"<table border=1 cellpadding=6 cellspacing=0>{header_html}{''.join(rows_html)}</table>"

    # Send grouped by teacher helper
    def _send_grouped(selected_indices: list[int]):
        # Group selected indices by teacher email
        grouped_by_email = {}
        for i in selected_indices:
            row = df_out.iloc[i]
            email_val = _fmt(row.get(col_email))
            if email_val:
                # Use first valid email as group key
                email_list = re.findall(r"[^@\s]+@[^@\s]+\.[^@\s]+", email_val)
                if email_list:
                    group_key = email_list[0].lower()
                    if group_key not in grouped_by_email:
                        grouped_by_email[group_key] = {
                            "indices": [],
                            "to": email_val,
                            "name": _fmt(row.get(col_teacher)),
                        }
                    grouped_by_email[group_key]["indices"].append(i)

        ok, fail = 0, []
        email_regex = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

        for group in grouped_by_email.values():
            idxs = group["indices"]
            if not idxs:
                continue

            varmap = _build_varmap(idxs[0])
            mons, lops = [], []
            for i in idxs:
                r = df_out.iloc[i]
                if col_subject:
                    v = _fmt(r.get(col_subject))
                    if v and v not in mons:
                        mons.append(v)
                if col_class:
                    v = _fmt(r.get(col_class))
                    if v and v not in lops:
                        lops.append(v)
            varmap["mon_hoc"] = ", ".join(mons)
            varmap["lop"] = ", ".join(lops)
            txt_table, html_table = _make_tables(idxs)
            varmap["lich_text"] = txt_table
            varmap["lich_html"] = html_table

            body_final = _render_tpl(mail_body, varmap)
            html_body_final = (
                _render_tpl(mail_body_html, varmap)
                if (mail_use_html and mail_body_html)
                else None
            )
            subject_final = _render_tpl(mail_subject, varmap)

            to_raw = group["to"]
            emails = email_regex.findall(to_raw or "")
            if not emails:
                fail.append((f"GV: {group['name']}", "Email không hợp lệ"))
                continue

            for addr in emails:
                ok_one, err = _send_email_gmail(
                    addr, subject_final, body_final, html_body_final
                )
                if ok_one:
                    ok += 1
                else:
                    fail.append((f"GV: {group['name']} ({addr})", err))
        return ok, fail

    def _send_individual(selected_indices: list[int]):
        ok, fail = 0, []
        for i in selected_indices:
            row = mail_view.iloc[i]
            to = _fmt(row.get("Email"))
            if not to:
                fail.append((f"Hàng {i+1}", "Thiếu email") )
                continue

            varmap = _build_varmap(i)
            body = _render_tpl(mail_body, varmap)
            html_body = (
                _render_tpl(mail_body_html, varmap)
                if (mail_use_html and mail_body_html)
                else None
            )
            subject_fmt = _render_tpl(mail_subject, varmap)

            emails = re.findall(r"[^@\s]+@[^@\s]+\.[^@\s]+", to or "")
            if not emails:
                fail.append((f"Hàng {i+1}", "Email không hợp lệ") )
                continue

            for addr in emails:
                ok_one, err = _send_email_gmail(addr, subject_fmt, body, html_body)
                if ok_one:
                    ok += 1
                else:
                    fail.append((f"Hàng {i+1} ({addr})", err))
        return ok, fail

    # Send button
    if send_col.button("Gửi email đã chọn"):
        if not selected_indices:
            st.warning("Vui lòng chọn ít nhất 1 người nhận")
        elif not st.session_state.get("gmail_creds"):
            st.error("Chưa kết nối Gmail. Vui lòng kết nối và thử lại.")
        else:
            st.info("Đang gửi email...")
            if group_send_by_teacher:
                ok, fail = _send_grouped(selected_indices)
                if ok:
                    st.success(f"Đã gửi {ok} email (gộp theo giảng viên) thành công.")
            else:
                ok, fail = _send_individual(selected_indices)
                if ok:
                    st.success(f"Đã gửi {ok} email thành công.")

            if fail:
                st.error("Lỗi khi gửi một số email:")
                for item, err in fail:
                    st.write(f"- {item}: {err}")

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
