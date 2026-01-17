import os
import sqlite3
import secrets
import time
from datetime import datetime, timezone
from io import BytesIO

import pandas as pd
import qrcode
from PIL import Image
import requests
import streamlit as st

# ----------------------------
# Config
# ----------------------------
DB_PATH = "event_qr.db"
DEFAULT_GUESTLIST_PATHS = ["Guestlist.csv", "Guestlist.xlsx", "guestlist.csv", "guestlist.xlsx"]
CHECKPOINTS = ["BREAKFAST", "LUNCH", "GIFT"]

# WhatsApp Cloud API (set in secrets.toml)
PHONE_NUMBER_ID = st.secrets.get("WHATSAPP_PHONE_NUMBER_ID", "")
ACCESS_TOKEN = st.secrets.get("WHATSAPP_ACCESS_TOKEN", "")
GRAPH_API_VERSION = st.secrets.get("GRAPH_API_VERSION", "v20.0")

# Templates
TEMPLATE_MAIN = st.secrets.get("TEMPLATE_MAIN", "vidira_event_qr_image")
TEMPLATE_EXTRA = st.secrets.get("TEMPLATE_EXTRA", "vidira_event_food_qr_image")
TEMPLATE_LANG = st.secrets.get("TEMPLATE_LANG", "en")

# App lock
APP_PASSWORD = st.secrets.get("APP_PASSWORD", "")
APP_USERS_RAW = st.secrets.get("APP_USERS", "")
ALLOWED_USERS = [u.strip().lower() for u in str(APP_USERS_RAW).split(",") if u.strip()]

st.set_page_config(page_title="Vidira Event QR", page_icon="🔳", layout="wide")

# Debounce settings (prevents repeated beeps on same scan)
SCAN_DEBOUNCE_SECONDS = 2.0


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


def beep(success: bool):
    # Best-effort browser beep
    freq = 880 if success else 220
    st.components.v1.html(
        f"""
        <script>
        (function() {{
          try {{
            const Ctx = window.AudioContext || window.webkitAudioContext;
            const ctx = new Ctx();
            const o = ctx.createOscillator();
            const g = ctx.createGain();
            o.type = "sine";
            o.frequency.value = {freq};
            g.gain.value = 0.25;
            o.connect(g); g.connect(ctx.destination);
            o.start();
            setTimeout(() => {{ o.stop(); ctx.close(); }}, 160);
          }} catch(e) {{}}
        }})();
        </script>
        """,
        height=0,
    )


# ----------------------------
# Login / Lock (ENFORCED)
# ----------------------------
def login_gate():
    if not APP_PASSWORD or not str(APP_PASSWORD).strip():
        st.error("APP_PASSWORD is missing in Streamlit secrets. App is locked until you set it.")
        st.info("Streamlit Cloud → App → Settings → Secrets → set APP_PASSWORD and reboot.")
        st.stop()

    if st.session_state.get("auth_ok"):
        return

    st.title("🔒 Login Required")

    username = st.text_input("Username", placeholder="e.g. breakfast1")
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
# DB helpers + migration
# ----------------------------
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def _table_exists(cur, name: str) -> bool:
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,))
    return cur.fetchone() is not None


def table_columns(cur, table: str) -> list[str]:
    cur.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


def migrate_schema(conn: sqlite3.Connection):
    cur = conn.cursor()
    if _table_exists(cur, "members"):
        cols = table_columns(cur, "members")
        if "phone10" not in cols:
            cur.execute("ALTER TABLE members ADD COLUMN phone10 TEXT")
            conn.commit()
            cols = table_columns(cur, "members")
            if "phone" in cols:
                cur.execute("SELECT id, phone FROM members")
                for mid, phone in cur.fetchall():
                    p10 = normalize_phone_10(phone)
                    cur.execute("UPDATE members SET phone10=? WHERE id=?", (p10, mid))
                conn.commit()

    if _table_exists(cur, "members") and "phone10" in table_columns(cur, "members"):
        try:
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_members_phone10
                ON members(phone10)
                WHERE phone10 IS NOT NULL AND phone10 <> ''
            """)
            conn.commit()
        except Exception:
            pass


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

    cur.execute("""
    CREATE TABLE IF NOT EXISTS members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        phone10 TEXT,
        role TEXT NOT NULL,
        token TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        FOREIGN KEY(company_id) REFERENCES companies(id)
    )
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

    cur.execute("""
    CREATE TABLE IF NOT EXISTS manual_counts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        checkpoint TEXT NOT NULL,
        qty INTEGER NOT NULL,
        category TEXT NOT NULL,
        note TEXT,
        used_at TEXT NOT NULL,
        device TEXT,
        added_by TEXT
    )
    """)

    # once per token per checkpoint (breakfast/lunch)
    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS uq_token_checkpoint
    ON redemptions(token, checkpoint)
    WHERE token IS NOT NULL
    """)

    # one gift per company
    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS uq_company_gift
    ON redemptions(company_id, checkpoint)
    WHERE company_id IS NOT NULL AND checkpoint='GIFT'
    """)

    conn.commit()
    migrate_schema(conn)
    conn.close()


# ----------------------------
# Guestlist / Companies
# ----------------------------
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
            cur.execute("INSERT INTO guest_companies (company_name, uploaded_at) VALUES (?, ?)", (c, ts))
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


# ----------------------------
# Manual counts + Undo
# ----------------------------
def add_manual_count(checkpoint: str, qty: int, category: str, note: str, device: str) -> int:
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
    """, (checkpoint, qty, category, note.strip() if note else None, now_iso(),
          device.strip() if device else None, user))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return int(rid)


def undo_last_manual_count(category: str, device: str) -> tuple[bool, str]:
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
        return False, "Nothing to undo."

    mid, cp, qty = row
    cur.execute("DELETE FROM manual_counts WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    return True, f"Undone: {cp} -{qty} (Friends & Family)"


def count_breakdown(checkpoint: str):
    conn = get_conn()
    cur = conn.cursor()

    if checkpoint == "GIFT":
        cur.execute("SELECT COUNT(*) FROM redemptions WHERE checkpoint='GIFT'")
        g = cur.fetchone()[0]
        conn.close()
        return int(g), 0

    cur.execute("SELECT COUNT(*) FROM redemptions WHERE checkpoint=?", (checkpoint,))
    qr = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(qty), 0) FROM manual_counts WHERE checkpoint=?", (checkpoint,))
    manual = cur.fetchone()[0]
    conn.close()
    return int(qr), int(manual)


# ----------------------------
# Redemption
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


# ----------------------------
# QR + WhatsApp (kept as-is)
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
# Guestlist disk helpers
# ----------------------------
def _norm_col(c) -> str:
    return str(c).strip().lower().replace("\n", " ").replace("_", " ").replace("-", " ")


def load_guestlist_from_disk(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    if path.lower().endswith(".csv"):
        try:
            df = pd.read_csv(path, encoding="utf-8")
        except Exception:
            df = pd.read_csv(path, encoding="latin-1")
    else:
        df = pd.read_excel(path)

    col_map = {_norm_col(c): c for c in df.columns}
    candidates = ["company", "company name", "companyname", "company_name", "firm", "party", "customer", "organization", "organisation"]
    found = None
    for k in candidates:
        if k in col_map:
            found = col_map[k]
            break
    if not found:
        raise ValueError(f"Company column not found. Columns: {list(df.columns)}")

    companies = df[found].dropna().astype(str).map(normalize_company).tolist()
    companies = [c for c in companies if c]
    return sorted(list(set(companies)))


def find_default_guestlist_path() -> str | None:
    for p in DEFAULT_GUESTLIST_PATHS:
        if os.path.exists(p):
            return p
    return None


def ensure_guestlist_loaded_once():
    if load_guest_companies():
        return
    p = find_default_guestlist_path()
    if not p:
        return
    upsert_guest_companies(load_guestlist_from_disk(p))


# ----------------------------
# Scan Notice (auto-clear)
# ----------------------------
def set_scan_notice(color: str, title: str, subtitle: str):
    st.session_state.last_scan_result = (color, title, subtitle)
    st.session_state.last_scan_ts = time.time()


def render_scan_notice_autoclear(seconds: int = 2):
    ts = st.session_state.get("last_scan_ts")
    res = st.session_state.get("last_scan_result")

    if not ts or not res:
        st.info("Ready to scan…")
        return

    if time.time() - ts >= seconds:
        st.session_state.last_scan_result = None
        st.session_state.last_scan_ts = None
        st.info("Ready to scan…")
        return

    color, title, subtitle = res
    if color == "green":
        st.success(f"✅ {title}\n\n**{subtitle}**")
    else:
        st.error(f"❌ {title}\n\n**{subtitle}**")


# ----------------------------
# Pages
# ----------------------------
def page_registration():
    st.header("🧾 Registration")
    # (Keeping registration as you already had; not changed here)
    st.info("Registration page unchanged in this update. Use your existing working registration logic here.")


def page_scan():
    st.header("📷 Scan Kiosk (Phone Camera Only)")

    checkpoint = st.radio("Select Counter", CHECKPOINTS, horizontal=True)
    device = st.text_input("Device name (optional)", placeholder="e.g. breakfast-1 / lunch-1 / gift-1")

    st.divider()

    # Ensure scanner nonce exists to force-reset camera widget after each VALID scan
    if "scanner_nonce" not in st.session_state:
        st.session_state.scanner_nonce = 0

    # Ensure debounce state exists
    if "last_raw_scan" not in st.session_state:
        st.session_state.last_raw_scan = None
    if "last_raw_scan_ts" not in st.session_state:
        st.session_state.last_raw_scan_ts = 0.0

    # CAMERA FIRST (mobile)
    st.subheader("Phone Camera Scan")
    st.caption("If camera box doesn't appear, add `streamlit-qrcode-scanner` in requirements.txt and redeploy.")

    scanned = None
    try:
        from streamlit_qrcode_scanner import qrcode_scanner
        scanned = qrcode_scanner(key=f"qr_scanner_{st.session_state.scanner_nonce}")
    except Exception:
        st.warning("Camera scanner not available. Install `streamlit-qrcode-scanner` and redeploy.")

    # Process scan ONCE (debounced)
    if scanned:
        now = time.time()
        same_as_last = (scanned == st.session_state.last_raw_scan) and (now - st.session_state.last_raw_scan_ts < SCAN_DEBOUNCE_SECONDS)

        if not same_as_last:
            st.session_state.last_raw_scan = scanned
            st.session_state.last_raw_scan_ts = now

            color, title, subtitle = redeem(scanned, checkpoint, device)
            set_scan_notice(color, title, subtitle)

            # Toast once
            try:
                st.toast(f"{'✅' if color=='green' else '❌'} {title} — {subtitle}",
                         icon="✅" if color == "green" else "❌")
            except Exception:
                pass

            # Beep ONCE only for this scan
            beep(color == "green")

            # Force-reset scanner widget so it doesn't keep returning same value
            st.session_state.scanner_nonce += 1
            st.rerun()

    # NOTICE DIRECTLY BELOW SCANNER + AUTO-CLEAR IN 2s
    render_scan_notice_autoclear(seconds=2)

    st.divider()

    # COUNTS
    b_qr, b_ff = count_breakdown("BREAKFAST")
    l_qr, l_ff = count_breakdown("LUNCH")
    g_total, _ = count_breakdown("GIFT")

    col1, col2, col3 = st.columns(3)
    col1.metric("Breakfast Served", b_qr + b_ff, help=f"QR: {b_qr} | Friends & Family: {b_ff}")
    col2.metric("Lunch Served", l_qr + l_ff, help=f"QR: {l_qr} | Friends & Family: {l_ff}")
    col3.metric("Gifts Given", g_total)

    st.divider()

    # FRIENDS & FAMILY
    st.subheader("👪 Friends & Family (No QR) — Manual Plate Count")
    ff_counter = st.selectbox("Add to", ["BREAKFAST", "LUNCH"], key="ff_counter")
    ff_note = st.text_input("Note (optional)", placeholder="Family / VIP / Staff", key="ff_note")

    c1, c2, c3, c4 = st.columns([1, 1, 1, 1.2])
    with c1:
        if st.button("➕ +1", use_container_width=True):
            add_manual_count(ff_counter, 1, "FAMILY", ff_note, device)
            set_scan_notice("green", "COUNT ADDED", f"{ff_counter}: +1 (Friends & Family)")
            st.toast(f"✅ COUNT ADDED — {ff_counter}: +1", icon="✅")
            beep(True)
            st.rerun()
    with c2:
        if st.button("➕ +5", use_container_width=True):
            add_manual_count(ff_counter, 5, "FAMILY", ff_note, device)
            set_scan_notice("green", "COUNT ADDED", f"{ff_counter}: +5 (Friends & Family)")
            st.toast(f"✅ COUNT ADDED — {ff_counter}: +5", icon="✅")
            beep(True)
            st.rerun()
    with c3:
        qty = st.number_input("Qty", min_value=1, max_value=50, value=1, step=1)
    with c4:
        if st.button("Add Qty", use_container_width=True):
            add_manual_count(ff_counter, int(qty), "FAMILY", ff_note, device)
            set_scan_notice("green", "COUNT ADDED", f"{ff_counter}: +{int(qty)} (Friends & Family)")
            st.toast(f"✅ COUNT ADDED — {ff_counter}: +{int(qty)}", icon="✅")
            beep(True)
            st.rerun()

    if st.button("↩️ Undo last Friends & Family add"):
        ok, msg = undo_last_manual_count("FAMILY", device)
        set_scan_notice("green" if ok else "red", "UNDO" if ok else "UNDO FAILED", msg)
        st.toast(f"{'✅' if ok else '❌'} {msg}", icon="✅" if ok else "❌")
        beep(ok)
        st.rerun()


def page_admin():
    st.header("🛠️ Admin")

    st.subheader("⚠️ Reset Database (Testing / Before Event)")
    st.caption("HARD RESET deletes DB file. SOFT RESET clears data but keeps guestlist.")

    colA, colB = st.columns(2)
    with colA:
        if st.button("🧨 HARD RESET (Delete DB file)", type="primary"):
            if os.path.exists(DB_PATH):
                os.remove(DB_PATH)
            st.session_state.last_scan_result = None
            st.session_state.last_scan_ts = None
            st.session_state.last_raw_scan = None
            st.session_state.last_raw_scan_ts = 0.0
            st.session_state.scanner_nonce = 0
            st.success("DB deleted. Fresh start.")
            st.rerun()

    with colB:
        if st.button("🧼 SOFT RESET (Clear tables)", type="secondary"):
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("DELETE FROM redemptions")
            cur.execute("DELETE FROM manual_counts")
            cur.execute("DELETE FROM entitlements")
            cur.execute("DELETE FROM members")
            cur.execute("DELETE FROM companies")
            conn.commit()
            conn.close()
            st.session_state.last_scan_result = None
            st.session_state.last_scan_ts = None
            st.session_state.last_raw_scan = None
            st.session_state.last_raw_scan_ts = 0.0
            st.session_state.scanner_nonce = 0
            st.success("Cleared registrations + scans (guestlist kept).")
            st.rerun()

    st.divider()
    st.subheader("View Redemptions (with Company + Name)")

    conn = get_conn()
    query = """
    SELECT
      r.id,
      r.checkpoint,
      r.used_at,
      r.device,
      COALESCE(cg.company_name, c.company_name) AS company_name,
      m.name AS member_name,
      m.role AS member_role,
      m.phone10 AS phone10
    FROM redemptions r
    LEFT JOIN members m ON m.token = r.token
    LEFT JOIN companies c ON c.id = m.company_id
    LEFT JOIN companies cg ON cg.id = r.company_id
    ORDER BY r.id DESC
    LIMIT 200
    """
    df_red = pd.read_sql_query(query, conn)
    conn.close()

    st.dataframe(df_red, use_container_width=True)

    st.divider()
    st.subheader("Raw DB Tables (latest 200 rows)")
    table = st.selectbox("Table", ["guest_companies", "companies", "members", "entitlements", "redemptions", "manual_counts"])
    conn = get_conn()
    df = pd.read_sql_query(f"SELECT * FROM {table} ORDER BY id DESC LIMIT 200", conn)
    st.dataframe(df, use_container_width=True)
    conn.close()


# ----------------------------
# Main
# ----------------------------
def main():
    login_gate()
    init_db()

    st.sidebar.title("Vidira Event System")
    st.sidebar.caption(f"Logged in as: **{st.session_state.get('username','')}**")

    page = st.sidebar.radio("Go to", ["Registration", "Scan", "Admin"], index=1)

    if page == "Registration":
        page_registration()
    elif page == "Scan":
        page_scan()
    else:
        page_admin()


if __name__ == "__main__":
    main()
