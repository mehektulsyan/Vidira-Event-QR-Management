# app.py
# Vidira Event QR Management (Streamlit) — FINAL (with Undo Friends & Family)
#
# Features:
# - Login lock (shared password + optional allowed usernames)
# - Guestlist auto-load from Guestlist.csv/xlsx (backend)
# - Registration: company search + add company on-spot
# - Unlimited extra members via "Add more"
# - Phone validation: digits only, exactly 10 digits (India); blocks invalid
# - Duplicate phone detection: blocks if phone already registered; shows existing record
# - WhatsApp templates with IMAGE header (QR sent as image in template):
#     TEMPLATE_MAIN: Breakfast/Lunch/Gift
#     TEMPLATE_EXTRA: Breakfast/Lunch
# - Scan kiosk: hardware scanner + phone camera scanner
# - Full-screen GREEN/RED overlay + beep + big company/name
# - Counts: Breakfast/Lunch/Gifts separately + breakdown
# - Friends & Family manual plate count (Breakfast/Lunch) with full audit trail:
#     qty, timestamp, device, added_by, note
# - Undo last Friends & Family add (for accidental +5 / wrong counter)

import os
import sqlite3
import secrets
from datetime import datetime, timezone
from io import BytesIO

import pandas as pd
import qrcode
from PIL import Image
import requests
import streamlit as st
import streamlit.components.v1 as components

# ----------------------------
# Config
# ----------------------------
DB_PATH = "event_qr.db"
DEFAULT_GUESTLIST_PATHS = ["Guestlist.csv", "Guestlist.xlsx", "guestlist.csv", "guestlist.xlsx"]
CHECKPOINTS = ["BREAKFAST", "LUNCH", "GIFT"]

# WhatsApp Cloud API (set in .streamlit/secrets.toml)
PHONE_NUMBER_ID = st.secrets.get("WHATSAPP_PHONE_NUMBER_ID", "")
ACCESS_TOKEN = st.secrets.get("WHATSAPP_ACCESS_TOKEN", "")
GRAPH_API_VERSION = st.secrets.get("GRAPH_API_VERSION", "v20.0")

# Templates (set in secrets or fallback)
TEMPLATE_MAIN = st.secrets.get("TEMPLATE_MAIN", "vidira_event_qr_image")
TEMPLATE_EXTRA = st.secrets.get("TEMPLATE_EXTRA", "vidira_event_food_qr_image")
TEMPLATE_LANG = st.secrets.get("TEMPLATE_LANG", "en")

# App lock
APP_PASSWORD = st.secrets.get("APP_PASSWORD", "")
APP_USERS_RAW = st.secrets.get("APP_USERS", "")
ALLOWED_USERS = [u.strip().lower() for u in str(APP_USERS_RAW).split(",") if u.strip()]

st.set_page_config(page_title="Vidira Event QR", page_icon="🔳", layout="wide")


# ----------------------------
# Utilities
# ----------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_company(s: str) -> str:
    return " ".join(str(s).strip().split())


def digits_only(s: str) -> str:
    return "".join(ch for ch in str(s) if ch.isdigit())


def normalize_phone_10(phone: str) -> str:
    """
    Return 10-digit Indian phone number (local) or "" if invalid.
    Accepts:
      - 9830675002
      - +91 9830675002
      - 919830675002
    """
    d = digits_only(phone)
    if len(d) == 10:
        return d
    if len(d) == 12 and d.startswith("91"):
        return d[2:]
    return ""


def phone_to_e164_india(phone10: str) -> str:
    return "91" + phone10


def create_token() -> str:
    return secrets.token_urlsafe(16)


# ----------------------------
# Login / Lock
# ----------------------------
def login_gate():
    if not APP_PASSWORD:
        # Unlocked mode (not recommended)
        st.session_state.auth_ok = True
        st.session_state.username = st.session_state.get("username", "unlocked")
        return

    if st.session_state.get("auth_ok"):
        return

    st.title("🔒 Login Required")
    st.caption("Enter your username and the shared password to access the app.")

    username = st.text_input("Username", placeholder="e.g. reg1")
    password = st.text_input("Password", type="password", placeholder="Shared password")

    if st.button("Login", type="primary"):
        u = username.strip().lower()
        p = password.strip()

        if not u:
            st.error("Username is required.")
            st.stop()

        if ALLOWED_USERS and u not in ALLOWED_USERS:
            st.error("This username is not allowed.")
            st.stop()

        if p != APP_PASSWORD:
            st.error("Incorrect password.")
            st.stop()

        st.session_state.auth_ok = True
        st.session_state.username = u
        st.success("Logged in.")
        st.rerun()

    st.stop()


# ----------------------------
# DB helpers
# ----------------------------
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS guest_companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_name TEXT NOT NULL UNIQUE,
        uploaded_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_name TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    )
    """)

    # Members: phone10 UNIQUE to prevent duplicate QR issuance
    cur.execute("""
    CREATE TABLE IF NOT EXISTS members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        phone10 TEXT NOT NULL,
        role TEXT NOT NULL,          -- MAIN or EXTRA
        token TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        FOREIGN KEY(company_id) REFERENCES companies(id)
    )
    """)

    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS uq_members_phone10
    ON members(phone10)
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS entitlements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT NOT NULL,
        checkpoint TEXT NOT NULL,
        UNIQUE(token, checkpoint)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS redemptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT,
        company_id INTEGER,
        checkpoint TEXT NOT NULL,
        used_at TEXT NOT NULL,
        device TEXT
    )
    """)

    # Manual counts (Friends & Family)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS manual_counts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        checkpoint TEXT NOT NULL,     -- BREAKFAST or LUNCH only
        qty INTEGER NOT NULL,
        category TEXT NOT NULL,       -- e.g. 'FAMILY'
        note TEXT,
        used_at TEXT NOT NULL,
        device TEXT,
        added_by TEXT
    )
    """)

    # Breakfast/Lunch: once per token
    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS uq_token_checkpoint
    ON redemptions(token, checkpoint)
    WHERE token IS NOT NULL
    """)

    # Gift: once per company
    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS uq_company_gift
    ON redemptions(company_id, checkpoint)
    WHERE company_id IS NOT NULL AND checkpoint='GIFT'
    """)

    conn.commit()
    conn.close()


def load_guest_companies() -> list[str]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT company_name FROM guest_companies ORDER BY company_name ASC")
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]


def upsert_guest_companies(company_names: list[str]) -> int:
    conn = get_conn()
    cur = conn.cursor()
    ts = now_iso()
    inserted = 0
    for c in company_names:
        c = normalize_company(c)
        if not c:
            continue
        try:
            cur.execute(
                "INSERT INTO guest_companies (company_name, uploaded_at) VALUES (?, ?)",
                (c, ts),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    conn.close()
    return inserted


def company_in_guestlist(company_name: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM guest_companies WHERE company_name=? LIMIT 1", (normalize_company(company_name),))
    ok = cur.fetchone() is not None
    conn.close()
    return ok


def get_or_create_company(company_name: str) -> int:
    company_name = normalize_company(company_name)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM companies WHERE company_name=?", (company_name,))
    row = cur.fetchone()
    if row:
        conn.close()
        return row[0]
    cur.execute("INSERT INTO companies (company_name, created_at) VALUES (?, ?)", (company_name, now_iso()))
    conn.commit()
    cid = cur.lastrowid
    conn.close()
    return cid


def find_member_by_phone10(phone10: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT m.name, m.phone10, m.role, c.company_name, m.created_at
        FROM members m
        JOIN companies c ON c.id=m.company_id
        WHERE m.phone10=?
        LIMIT 1
    """, (phone10,))
    row = cur.fetchone()
    conn.close()
    return row


def add_member(company_id: int, name: str, phone10: str, role: str) -> str:
    token = create_token()
    conn = get_conn()
    cur = conn.cursor()
    for _ in range(6):
        try:
            cur.execute("""
                INSERT INTO members (company_id, name, phone10, role, token, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (company_id, name.strip(), phone10, role, token, now_iso()))
            conn.commit()
            conn.close()
            return token
        except sqlite3.IntegrityError:
            token = create_token()
    conn.close()
    raise RuntimeError("Failed generating unique token. Try again.")


def set_entitlements(token: str, checkpoints: list[str]):
    conn = get_conn()
    cur = conn.cursor()
    for cp in checkpoints:
        try:
            cur.execute("INSERT INTO entitlements (token, checkpoint) VALUES (?, ?)", (token, cp))
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    conn.close()


def token_entitled(token: str, checkpoint: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM entitlements WHERE token=? AND checkpoint=? LIMIT 1", (token.strip(), checkpoint))
    ok = cur.fetchone() is not None
    conn.close()
    return ok


def find_member_by_token(token: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT m.id, m.name, m.phone10, m.role, m.token, c.id, c.company_name
        FROM members m
        JOIN companies c ON c.id=m.company_id
        WHERE m.token=?
    """, (token.strip(),))
    row = cur.fetchone()
    conn.close()
    return row


def list_company_members(company_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, name, phone10, role, token, created_at
        FROM members
        WHERE company_id=?
        ORDER BY id ASC
    """, (company_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def add_manual_count(checkpoint: str, qty: int, category: str, note: str, device: str) -> int:
    """
    Inserts manual count row and returns inserted id.
    """
    if checkpoint not in ["BREAKFAST", "LUNCH"]:
        raise ValueError("Manual counts only allowed for BREAKFAST or LUNCH.")
    qty = int(qty)
    if qty <= 0:
        raise ValueError("qty must be >= 1")

    user = st.session_state.get("username", "unknown")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO manual_counts (checkpoint, qty, category, note, used_at, device, added_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        checkpoint,
        qty,
        category,
        note.strip() if note else None,
        now_iso(),
        device.strip() if device else None,
        user
    ))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return int(new_id)


def undo_last_manual_count(category: str, device: str) -> tuple[bool, str]:
    """
    Undo the most recent manual count entry for the current user (and device if provided).
    Returns (ok, message).
    """
    user = st.session_state.get("username", "unknown")
    conn = get_conn()
    cur = conn.cursor()

    if device and device.strip():
        cur.execute("""
            SELECT id, checkpoint, qty
            FROM manual_counts
            WHERE category=? AND added_by=? AND device=?
            ORDER BY id DESC
            LIMIT 1
        """, (category, user, device.strip()))
    else:
        cur.execute("""
            SELECT id, checkpoint, qty
            FROM manual_counts
            WHERE category=? AND added_by=?
            ORDER BY id DESC
            LIMIT 1
        """, (category, user))

    row = cur.fetchone()
    if not row:
        conn.close()
        return False, "Nothing to undo (no recent manual entries found)."

    mid, cp, qty = row
    cur.execute("DELETE FROM manual_counts WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    return True, f"Undone: {cp} -{qty} (Friends & Family)"


def count_breakdown(checkpoint: str):
    """
    Returns (qr_count, manual_count) for BREAKFAST/LUNCH.
    For GIFT: (gift_count, 0)
    """
    conn = get_conn()
    cur = conn.cursor()

    if checkpoint == "GIFT":
        cur.execute("SELECT COUNT(*) FROM redemptions WHERE checkpoint='GIFT'")
        qr = cur.fetchone()[0]
        conn.close()
        return int(qr), 0

    cur.execute("SELECT COUNT(*) FROM redemptions WHERE checkpoint=?", (checkpoint,))
    qr = cur.fetchone()[0]

    cur.execute("SELECT COALESCE(SUM(qty), 0) FROM manual_counts WHERE checkpoint=?", (checkpoint,))
    manual = cur.fetchone()[0]

    conn.close()
    return int(qr), int(manual)


def counts_for_checkpoint(checkpoint: str) -> int:
    qr, manual = count_breakdown(checkpoint)
    return int(qr + manual)


# ----------------------------
# Guestlist load from disk
# ----------------------------
def _norm_col(c) -> str:
    return str(c).strip().lower().replace("\n", " ").replace("_", " ").replace("-", " ")


def load_guestlist_from_disk(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    path_l = path.lower()
    if path_l.endswith(".csv"):
        try:
            df = pd.read_csv(path, encoding="utf-8")
        except Exception:
            df = pd.read_csv(path, encoding="latin-1")
    elif path_l.endswith(".xlsx") or path_l.endswith(".xls"):
        df = pd.read_excel(path)
    else:
        raise ValueError("Guestlist file must be .csv or .xlsx")

    col_map = {_norm_col(c): c for c in df.columns}
    candidates = [
        "company", "company name", "companyname", "company_name",
        "firm", "party", "party name", "customer", "customer name",
        "name of company", "organisation", "organization"
    ]

    found = None
    for k in candidates:
        if k in col_map:
            found = col_map[k]
            break

    if not found:
        for nc, orig in col_map.items():
            if "company" in nc and ("name" in nc or "nm" in nc):
                found = orig
                break

    if not found:
        raise ValueError(f"Guestlist loaded but company column not found. Columns: {list(df.columns)}")

    companies = (
        df[found]
        .dropna()
        .astype(str)
        .map(normalize_company)
        .tolist()
    )
    companies = [c for c in companies if c]
    return sorted(list(set(companies)))


def find_default_guestlist_path() -> str | None:
    for p in DEFAULT_GUESTLIST_PATHS:
        if os.path.exists(p):
            return p
    return None


def ensure_guestlist_loaded_once() -> int:
    if load_guest_companies():
        return 0
    path = find_default_guestlist_path()
    if not path:
        return 0
    companies = load_guestlist_from_disk(path)
    return upsert_guest_companies(companies)


# ----------------------------
# QR helpers
# ----------------------------
def make_qr_png_bytes(data: str) -> bytes:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=3,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    if not isinstance(img, Image.Image):
        img = img.convert("RGB")
    img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ----------------------------
# WhatsApp Cloud API (Template with Image Header)
# ----------------------------
def _wa_ready() -> bool:
    return bool(PHONE_NUMBER_ID and ACCESS_TOKEN and GRAPH_API_VERSION)


def wa_upload_media(png_bytes: bytes) -> str:
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/media"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    files = {"file": ("qr.png", png_bytes, "image/png")}
    data = {"messaging_product": "whatsapp"}
    r = requests.post(url, headers=headers, files=files, data=data, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Media upload failed [{r.status_code}]: {r.text}")
    return r.json()["id"]


def wa_send_template_with_image_header(
    to_e164: str,
    template_name: str,
    lang_code: str,
    header_image_media_id: str,
    body_params: list[str],
):
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_e164,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": lang_code},
            "components": [
                {"type": "header", "parameters": [{"type": "image", "image": {"id": header_image_media_id}}]},
                {"type": "body", "parameters": [{"type": "text", "text": p} for p in body_params]},
            ],
        },
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Send template failed [{r.status_code}]: {r.text}")
    return r.json()


# ----------------------------
# Kiosk UI: Full-screen overlay + beep
# ----------------------------
def kiosk_fullscreen_result(color: str, title: str, subtitle: str, beep: bool = True):
    palette = {
        "green": ("#0f5132", "#00c853"),
        "red": ("#842029", "#ff1744"),
        "blue": ("#084298", "#0d6efd"),
    }
    fg, accent = palette.get(color, ("#0c5460", "#17a2b8"))

    beep_js = ""
    if beep:
        freq = 880 if color == "green" else (220 if color == "red" else 520)
        beep_js = f"""
        <script>
          (function() {{
            try {{
              const Ctx = window.AudioContext || window.webkitAudioContext;
              const ctx = new Ctx();
              const o = ctx.createOscillator();
              const g = ctx.createGain();
              o.type = "sine";
              o.frequency.value = {freq};
              g.gain.value = 0.22;
              o.connect(g); g.connect(ctx.destination);
              o.start();
              setTimeout(() => {{ o.stop(); ctx.close(); }}, 180);
            }} catch(e) {{}}
          }})();
        </script>
        """

    html = f"""
    <div id="kioskOverlay" style="
        position: fixed; inset: 0;
        background: {accent};
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 999999;
        padding: 22px;
    ">
      <div style="
          width: min(1100px, 96vw);
          background: rgba(255,255,255,0.94);
          border-radius: 26px;
          padding: 30px 22px;
          box-shadow: 0 18px 60px rgba(0,0,0,0.25);
          text-align: center;
      ">
        <div style="font-size: 76px; font-weight: 1000; color: {fg}; line-height: 1;">
          {title}
        </div>
        <div style="font-size: 38px; font-weight: 900; color: {fg}; margin-top: 14px;">
          {subtitle}
        </div>
        <div style="font-size: 16px; font-weight: 800; color: rgba(0,0,0,0.55); margin-top: 16px;">
          (Ready for next scan)
        </div>
      </div>
    </div>
    {beep_js}
    <script>
      setTimeout(() => {{
        const el = document.getElementById("kioskOverlay");
        if (el) el.remove();
      }}, 2000);
    </script>
    """
    components.html(html, height=0)


# ----------------------------
# Scan / Redemption
# ----------------------------
def redeem(token: str, checkpoint: str, device: str = ""):
    token = (token or "").strip().replace("\n", "").replace("\r", "")
    m = find_member_by_token(token)
    if not m:
        return ("red", "INVALID", "Token not found")

    _, name, _phone10, _role, _token, company_id, company_name = m

    if not token_entitled(token, checkpoint):
        return ("red", "NOT ALLOWED", f"{company_name} — {name}")

    conn = get_conn()
    cur = conn.cursor()

    if checkpoint == "GIFT":
        try:
            cur.execute("""
                INSERT INTO redemptions(token, company_id, checkpoint, used_at, device)
                VALUES (NULL, ?, 'GIFT', ?, ?)
            """, (company_id, now_iso(), device))
            conn.commit()
            conn.close()
            return ("green", "APPROVED", f"{company_name} — {name}")
        except sqlite3.IntegrityError:
            conn.close()
            return ("red", "USED ALREADY", f"{company_name} — {name}")

    try:
        cur.execute("""
            INSERT INTO redemptions(token, company_id, checkpoint, used_at, device)
            VALUES (?, NULL, ?, ?, ?)
        """, (token, checkpoint, now_iso(), device))
        conn.commit()
        conn.close()
        return ("green", "APPROVED", f"{company_name} — {name}")
    except sqlite3.IntegrityError:
        conn.close()
        return ("red", "USED ALREADY", f"{company_name} — {name}")


def camera_scan_component():
    """
    Returns scanned text or None.
    Ensure `streamlit-qrcode-scanner` is in requirements.txt for Streamlit Cloud.
    """
    try:
        from streamlit_qrcode_scanner import qrcode_scanner
        return qrcode_scanner(key="qr_scanner")
    except Exception as e:
        st.warning(f"Camera scanner not available. Ensure `streamlit-qrcode-scanner` is installed. ({e})")
        return None


# ----------------------------
# Pages
# ----------------------------
def company_search_select(guest_companies: list[str]) -> str:
    st.subheader("Company")
    q = st.text_input("Type to search company", placeholder="Start typing…")
    filtered = guest_companies
    if q.strip():
        qq = q.strip().lower()
        filtered = [c for c in guest_companies if qq in c.lower()]
    if not filtered:
        st.warning("No match found in guestlist for your search.")
        return ""
    return st.selectbox("Select company", filtered)


def page_registration():
    st.header("🧾 Registration")

    guest_companies = load_guest_companies()
    allow_spot_company = st.toggle("Allow adding company on the spot", value=True)

    if guest_companies:
        company = company_search_select(guest_companies)

        if allow_spot_company:
            with st.expander("Add a company not in the list"):
                new_company = st.text_input("New company name", placeholder="Type new company name")
                if st.button("Add company to guestlist"):
                    nc = normalize_company(new_company)
                    if not nc:
                        st.error("Company name cannot be empty.")
                    else:
                        added = upsert_guest_companies([nc])
                        st.success(f"Added '{nc}' to guestlist." if added else f"'{nc}' already exists.")
                        st.rerun()
    else:
        st.warning("Guestlist is empty. Add companies in Admin or add on the spot.")
        company = st.text_input("Company name *", placeholder="Type company name")

    company_norm = normalize_company(company)
    st.divider()

    st.subheader("Main Member (Breakfast + Lunch + Gift)")
    main_name = st.text_input("Main member name *", placeholder="Full name")
    main_phone_raw = st.text_input("Main member phone * (10 digits)", placeholder="e.g. 9830675002")

    st.divider()
    st.subheader("Additional Members (Breakfast + Lunch only)")

    if "extra_count" not in st.session_state:
        st.session_state.extra_count = 0

    c1, c2, c3 = st.columns([1, 1, 4])
    with c1:
        if st.button("➕ Add more"):
            st.session_state.extra_count += 1
    with c2:
        if st.session_state.extra_count > 0 and st.button("➖ Remove last"):
            st.session_state.extra_count -= 1
    with c3:
        st.caption("Add as many extra representatives as needed.")

    extras_raw = []
    for i in range(st.session_state.extra_count):
        st.markdown(f"**Extra member #{i+1}**")
        n = st.text_input("Name *", key=f"ex_name_{i}")
        p = st.text_input("Phone * (10 digits)", key=f"ex_phone_{i}")
        extras_raw.append((n, p))

    st.divider()

    if not _wa_ready():
        st.warning("WhatsApp API not configured. Add WHATSAPP_PHONE_NUMBER_ID / WHATSAPP_ACCESS_TOKEN in secrets.toml.")

    if st.button("✅ Submit + Send WhatsApp QRs", type="primary"):
        if not company_norm:
            st.error("Company is required.")
            return

        if not main_name.strip():
            st.error("Main member name is required.")
            return

        main_phone10 = normalize_phone_10(main_phone_raw)
        if not main_phone10:
            st.error("Main phone must be exactly 10 digits (or include +91).")
            return

        existing = find_member_by_phone10(main_phone10)
        if existing:
            ex_name, ex_phone10, ex_role, ex_company, ex_created = existing
            st.error(
                f"QR already issued for this phone.\n\n"
                f"**Name:** {ex_name}\n\n"
                f"**Company:** {ex_company}\n\n"
                f"**Role:** {ex_role}\n\n"
                f"**Registered at:** {ex_created}"
            )
            return

        seen_phones = {main_phone10}
        extra_norm = []
        for n, p in extras_raw:
            if not n.strip():
                st.error("All extra member names are required.")
                return

            p10 = normalize_phone_10(p)
            if not p10:
                st.error("Each extra phone must be exactly 10 digits (or include +91).")
                return

            if p10 in seen_phones:
                st.error("Same phone number entered twice in this registration.")
                return

            existing2 = find_member_by_phone10(p10)
            if existing2:
                ex_name, ex_phone10, ex_role, ex_company, ex_created = existing2
                st.error(
                    f"QR already issued for this extra phone.\n\n"
                    f"**Name:** {ex_name}\n\n"
                    f"**Company:** {ex_company}\n\n"
                    f"**Role:** {ex_role}\n\n"
                    f"**Registered at:** {ex_created}"
                )
                return

            seen_phones.add(p10)
            extra_norm.append((n.strip(), p10))

        if not company_in_guestlist(company_norm):
            if allow_spot_company:
                upsert_guest_companies([company_norm])
                st.warning(f"Company '{company_norm}' was not in guestlist — added on spot.")
            else:
                st.error(f"Company '{company_norm}' not in guestlist. Enable on-spot add or add in Admin.")
                return

        company_id = get_or_create_company(company_norm)

        main_token = add_member(company_id, main_name, main_phone10, "MAIN")
        set_entitlements(main_token, ["BREAKFAST", "LUNCH", "GIFT"])

        extra_records = []
        for n, p10 in extra_norm:
            t = add_member(company_id, n, p10, "EXTRA")
            set_entitlements(t, ["BREAKFAST", "LUNCH"])
            extra_records.append((n, p10, t))

        st.success("Registered. Now generating QRs and sending WhatsApp template messages…")

        if not _wa_ready():
            st.error("WhatsApp API not configured; registration saved in DB but messages not sent.")
            st.info(f"MAIN token: {main_token}")
            for n, p10, t in extra_records:
                st.info(f"EXTRA {n} token: {t}")
            return

        try:
            to = phone_to_e164_india(main_phone10)
            png = make_qr_png_bytes(main_token)
            media_id = wa_upload_media(png)
            wa_send_template_with_image_header(
                to_e164=to,
                template_name=TEMPLATE_MAIN,
                lang_code=TEMPLATE_LANG,
                header_image_media_id=media_id,
                body_params=[main_name, company_norm],
            )
            st.success(f"WhatsApp QR sent to MAIN: {main_name} ({to})")
        except Exception as e:
            st.error(f"Failed sending MAIN WhatsApp: {e}")

        for n, p10, t in extra_records:
            try:
                to = phone_to_e164_india(p10)
                png = make_qr_png_bytes(t)
                media_id = wa_upload_media(png)
                wa_send_template_with_image_header(
                    to_e164=to,
                    template_name=TEMPLATE_EXTRA,
                    lang_code=TEMPLATE_LANG,
                    header_image_media_id=media_id,
                    body_params=[n, company_norm],
                )
                st.success(f"WhatsApp QR sent to EXTRA: {n} ({to})")
            except Exception as e:
                st.error(f"Failed sending EXTRA WhatsApp to {n}: {e}")

        with st.expander("View members created for this company"):
            rows = list_company_members(company_id)
            for _rid, nm, ph10, role, tok, created in rows:
                st.write(f"- **{role}** {nm} ({ph10}) — token `{tok}` — {created}")


def page_scan():
    st.markdown("## 📷 Scan Kiosk (Idiot-Proof)")

    checkpoint = st.radio("Counter", CHECKPOINTS, horizontal=True)
    device = st.text_input("Device name (optional)", placeholder="e.g. Breakfast-1 / Lunch-1 / Gift-1")

    b_qr, b_ff = count_breakdown("BREAKFAST")
    l_qr, l_ff = count_breakdown("LUNCH")
    g_total = counts_for_checkpoint("GIFT")

    col1, col2, col3 = st.columns(3)
    col1.metric("Breakfast Served", b_qr + b_ff, help=f"QR: {b_qr} | Friends & Family: {b_ff}")
    col2.metric("Lunch Served", l_qr + l_ff, help=f"QR: {l_qr} | Friends & Family: {l_ff}")
    col3.metric("Gifts Given", g_total)

    st.divider()

    # Friends & Family manual count + Undo
    st.subheader("👪 Friends & Family (No QR) — Manual Plate Count")
    st.caption("Tap +1 when someone without QR takes a plate. They may come separately. Every tap is recorded.")

    ff_counter = st.selectbox("Add to", ["BREAKFAST", "LUNCH"], key="ff_counter")
    ff_note = st.text_input("Note (optional)", placeholder="e.g. Family / Friends / VIP / Staff", key="ff_note")

    m1, m2, m3, m4, m5 = st.columns([1, 1, 1, 2, 1.2])

    with m1:
        if st.button("➕ +1", use_container_width=True):
            new_id = add_manual_count(ff_counter, 1, "FAMILY", ff_note, device)
            st.session_state.last_manual_id = new_id
            kiosk_fullscreen_result("green", "COUNT ADDED", f"{ff_counter}: +1 (Friends & Family)", beep=True)
            st.rerun()

    with m2:
        if st.button("➕ +5", use_container_width=True):
            new_id = add_manual_count(ff_counter, 5, "FAMILY", ff_note, device)
            st.session_state.last_manual_id = new_id
            kiosk_fullscreen_result("green", "COUNT ADDED", f"{ff_counter}: +5 (Friends & Family)", beep=True)
            st.rerun()

    with m3:
        ff_qty = st.number_input("Custom qty", min_value=1, max_value=50, value=1, step=1, key="ff_qty")

    with m4:
        if st.button("Add Custom Qty", type="secondary", use_container_width=True):
            new_id = add_manual_count(ff_counter, int(ff_qty), "FAMILY", ff_note, device)
            st.session_state.last_manual_id = new_id
            kiosk_fullscreen_result("green", "COUNT ADDED", f"{ff_counter}: +{int(ff_qty)} (Friends & Family)", beep=True)
            st.rerun()

    with m5:
        if st.button("↩️ Undo last", use_container_width=True):
            ok, msg = undo_last_manual_count("FAMILY", device)
            kiosk_fullscreen_result("blue" if ok else "red", "UNDO" if ok else "UNDO FAILED", msg, beep=True)
            st.rerun()

    st.divider()

    # Scan handlers
    def handle_token(token: str):
        color, title, subtitle = redeem(token, checkpoint, device)
        kiosk_fullscreen_result("green" if color == "green" else "red", title, subtitle, beep=True)

    # A) Hardware scanner
    st.subheader("A) Hardware Scanner")
    st.caption("Click the scan box once, then keep scanning. Scanner should be Keyboard/HID mode + ENTER suffix.")

    if "scan_buf" not in st.session_state:
        st.session_state.scan_buf = ""

    def on_scanned_input():
        raw = st.session_state.scan_buf
        st.session_state.scan_buf = ""
        if raw and raw.strip():
            handle_token(raw)

    st.text_input(
        "Scan token here (keep focused)",
        key="scan_buf",
        placeholder="Tap here once, then scan QR…",
        on_change=on_scanned_input,
    )

    st.divider()

    # B) Phone camera scan
    st.subheader("B) Phone Camera Scan")
    st.caption("If camera box doesn't appear, add `streamlit-qrcode-scanner` to requirements.txt and redeploy.")
    scanned = camera_scan_component()
    if scanned:
        handle_token(scanned)


def page_admin():
    st.header("🛠️ Admin")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM guest_companies")
    g = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM companies")
    c = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM members")
    m = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM redemptions")
    r = cur.fetchone()[0]
    conn.close()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Guestlist Companies", g)
    col2.metric("Registered Companies", c)
    col3.metric("Members", m)
    col4.metric("Total Redemptions", r)

    st.divider()

    b_qr, b_ff = count_breakdown("BREAKFAST")
    l_qr, l_ff = count_breakdown("LUNCH")
    g_total = counts_for_checkpoint("GIFT")

    colA, colB, colC = st.columns(3)
    colA.metric("Breakfast Served", b_qr + b_ff, help=f"QR: {b_qr} | Friends & Family: {b_ff}")
    colB.metric("Lunch Served", l_qr + l_ff, help=f"QR: {l_qr} | Friends & Family: {l_ff}")
    colC.metric("Gifts Given", g_total)

    st.divider()

    st.subheader("Add companies manually (one per line)")
    new_companies_text = st.text_area("Companies", placeholder="ABC Motors\nXYZ Spares\n...")
    if st.button("Add companies"):
        names = [normalize_company(x) for x in new_companies_text.splitlines()]
        names = [x for x in names if x]
        added = upsert_guest_companies(names)
        st.success(f"Added {added} new companies.")

    st.divider()

    st.subheader("Reload guestlist from backend file")
    path = find_default_guestlist_path()
    st.write(f"Detected guestlist file: **{path or 'None found'}**")
    if st.button("Force reload from file (merge into DB)"):
        if not path:
            st.error("No Guestlist.csv / Guestlist.xlsx found in the project folder.")
        else:
            try:
                companies = load_guestlist_from_disk(path)
                added = upsert_guest_companies(companies)
                st.success(f"Loaded {len(companies)} companies from {path}. Newly inserted: {added}")
            except Exception as e:
                st.error(f"Reload failed: {e}")

    st.divider()

    st.subheader("View DB tables (latest 200 rows)")
    table = st.selectbox("Table", ["guest_companies", "companies", "members", "entitlements", "redemptions", "manual_counts"])
    conn = get_conn()
    df = pd.read_sql_query(f"SELECT * FROM {table} ORDER BY id DESC LIMIT 200", conn)
    st.dataframe(df, use_container_width=True)
    conn.close()

    st.divider()

    st.subheader("WhatsApp API status")
    st.write(f"PHONE_NUMBER_ID present: **{bool(PHONE_NUMBER_ID)}**")
    st.write(f"ACCESS_TOKEN present: **{bool(ACCESS_TOKEN)}**")
    st.write(f"GRAPH_API_VERSION: **{GRAPH_API_VERSION}**")
    st.write(f"MAIN template: **{TEMPLATE_MAIN}** ({TEMPLATE_LANG})")
    st.write(f"EXTRA template: **{TEMPLATE_EXTRA}** ({TEMPLATE_LANG})")


# ----------------------------
# Main
# ----------------------------
def main():
    login_gate()
    init_db()

    inserted = 0
    try:
        inserted = ensure_guestlist_loaded_once()
    except Exception as e:
        st.sidebar.error(f"Guestlist auto-load failed: {e}")

    st.sidebar.title("Vidira Event System")
    st.sidebar.caption(f"Logged in as: **{st.session_state.get('username','')}**")
    if inserted:
        st.sidebar.success(f"Guestlist auto-loaded: {inserted} companies")

    page = st.sidebar.radio("Go to", ["Registration", "Scan", "Admin"], index=0)

    if page == "Registration":
        page_registration()
    elif page == "Scan":
        page_scan()
    else:
        page_admin()


if __name__ == "__main__":
    main()
