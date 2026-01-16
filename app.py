# app.py
# Vidira Event Registration + WhatsApp Template (Image Header) QR + Counter Scan
#
# Key features:
# - Guestlist auto-loads from backend file (Guestlist.csv / Guestlist.xlsx) to avoid upload issues
# - Registration: search & select company (no scrolling), and can add company on-spot
# - Capture name + phone for Main + up to 2 Extra members
# - Generate QR per member, upload to WhatsApp media, send via TEMPLATE with IMAGE header:
#     MAIN template:  vidira_event_qr_image  (Breakfast/Lunch/Gift)
#     EXTRA template: vidira_event_food_qr_image (Breakfast/Lunch)
# - Scanning at counters: BIG GREEN if approved, BIG RED if used/invalid/not entitled
#
# Requirements:
#   pip install streamlit pandas openpyxl qrcode pillow requests
# Optional camera scan:
#   pip install streamlit-qrcode-scanner
#
# Secrets (.streamlit/secrets.toml):
#   WHATSAPP_PHONE_NUMBER_ID="..."
#   WHATSAPP_ACCESS_TOKEN="..."
#   GRAPH_API_VERSION="v20.0"   # optional

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

# WhatsApp Cloud API settings (set in .streamlit/secrets.toml)
PHONE_NUMBER_ID = st.secrets.get("WHATSAPP_PHONE_NUMBER_ID", "")
ACCESS_TOKEN = st.secrets.get("WHATSAPP_ACCESS_TOKEN", "")
GRAPH_API_VERSION = st.secrets.get("GRAPH_API_VERSION", "v20.0")

# Templates you created
TEMPLATE_MAIN = "vidira_event_qr_image"
TEMPLATE_EXTRA = "vidira_event_food_qr_image"
TEMPLATE_LANG = "en"

st.set_page_config(page_title="Vidira Event QR", page_icon="🔳", layout="wide")


# ----------------------------
# Utilities
# ----------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_company(s: str) -> str:
    return " ".join(str(s).strip().split())


def normalize_phone_e164_india(phone: str) -> str:
    """
    Minimal India normalizer:
    - "98xxxxxxxx" -> "9198xxxxxxxx"
    - "9198xxxxxxxx" -> "9198xxxxxxxx"
    - Otherwise returns digits as-is
    """
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if digits.startswith("91") and len(digits) == 12:
        return digits
    if len(digits) == 10:
        return "91" + digits
    return digits


def create_token() -> str:
    return secrets.token_urlsafe(16)


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

    cur.execute("""
    CREATE TABLE IF NOT EXISTS members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        phone TEXT NOT NULL,
        role TEXT NOT NULL,          -- MAIN or EXTRA
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


def add_member(company_id: int, name: str, phone: str, role: str) -> str:
    token = create_token()
    conn = get_conn()
    cur = conn.cursor()

    for _ in range(5):
        try:
            cur.execute("""
                INSERT INTO members (company_id, name, phone, role, token, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (company_id, name.strip(), phone.strip(), role, token, now_iso()))
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
        SELECT m.id, m.name, m.phone, m.role, m.token, c.id, c.company_name
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
        SELECT id, name, phone, role, token, created_at
        FROM members
        WHERE company_id=?
        ORDER BY id ASC
    """, (company_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


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
        "company",
        "company name",
        "companyname",
        "company_name",
        "firm",
        "party",
        "party name",
        "customer",
        "customer name",
        "name of company",
        "organisation",
        "organization",
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
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": to_e164,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": lang_code},
            "components": [
                {
                    "type": "header",
                    "parameters": [
                        {"type": "image", "image": {"id": header_image_media_id}}
                    ],
                },
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in body_params],
                },
            ],
        },
    }

    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Send template failed [{r.status_code}]: {r.text}")
    return r.json()


# ----------------------------
# Scan / Redemption
# ----------------------------
def redeem(token: str, checkpoint: str, device: str = ""):
    token = token.strip().replace("\n", "").replace("\r", "")
    m = find_member_by_token(token)
    if not m:
        return ("red", "INVALID", "Token not found")

    _, name, phone, role, _, company_id, company_name = m

    if not token_entitled(token, checkpoint):
        return ("red", "NOT ENTITLED", f"{company_name} / {name} not eligible for {checkpoint}")

    conn = get_conn()
    cur = conn.cursor()

    # Gift once per company
    if checkpoint == "GIFT":
        try:
            cur.execute("""
                INSERT INTO redemptions(token, company_id, checkpoint, used_at, device)
                VALUES (NULL, ?, 'GIFT', ?, ?)
            """, (company_id, now_iso(), device))
            conn.commit()
            conn.close()
            return ("green", "APPROVED", f"GIFT OK — {company_name}")
        except sqlite3.IntegrityError:
            conn.close()
            return ("red", "USED ALREADY", f"GIFT already given to {company_name}")

    # Breakfast/Lunch once per token
    try:
        cur.execute("""
            INSERT INTO redemptions(token, company_id, checkpoint, used_at, device)
            VALUES (?, NULL, ?, ?, ?)
        """, (token, checkpoint, now_iso(), device))
        conn.commit()
        conn.close()
        return ("green", "APPROVED", f"{checkpoint} OK — {company_name} / {name}")
    except sqlite3.IntegrityError:
        conn.close()
        return ("red", "USED ALREADY", f"{checkpoint} already used — {company_name} / {name}")


def big_box(color: str, title: str, subtitle: str):
    palette = {
        "green": ("#0f5132", "#d1e7dd"),
        "red": ("#842029", "#f8d7da"),
        "yellow": ("#664d03", "#fff3cd"),
    }
    fg, bg = palette.get(color, ("#0c5460", "#d1ecf1"))
    html = f"""
    <div style="padding:24px;border-radius:14px;background:{bg};border:2px solid rgba(0,0,0,0.1);margin:10px 0 16px 0;">
      <div style="font-size:52px;font-weight:900;color:{fg};line-height:1.0;">{title}</div>
      <div style="font-size:22px;font-weight:700;color:{fg};margin-top:10px;">{subtitle}</div>
    </div>
    """
    components.html(html, height=190)


def camera_scan_component():
    try:
        from streamlit_qrcode_scanner import qrcode_scanner
        return qrcode_scanner(key="qr_scanner")
    except Exception:
        return None


# ----------------------------
# Pages
# ----------------------------
def company_picker(guest_companies: list[str]) -> str:
    """
    Provides:
    - text search filter
    - selectbox of filtered results
    """
    st.subheader("Company")
    q = st.text_input("Type to search company", placeholder="Start typing…")

    filtered = guest_companies
    if q.strip():
        qq = q.strip().lower()
        filtered = [c for c in guest_companies if qq in c.lower()]

    if not filtered:
        st.warning("No matches found in guestlist for your search.")
        return ""

    # selectbox is still useful (and searchable), but now list is small
    return st.selectbox("Select company", filtered)


def page_registration():
    st.header("🧾 Registration (Search company → Add members → Send WhatsApp QR images)")

    guest_companies = load_guest_companies()

    colA, colB = st.columns([2, 1])
    with colB:
        allow_spot_company = st.toggle("Allow adding company on the spot", value=True)

    if not guest_companies:
        st.warning("Guestlist is empty. Add companies in Admin or add on the spot.")
        company = st.text_input("Company name *", placeholder="Type company name")
    else:
        company = company_picker(guest_companies)

        if allow_spot_company:
            with st.expander("Add a company not in the list"):
                new_company = st.text_input("New company name", placeholder="Type new company name")
                if st.button("Add company to guestlist"):
                    nc = normalize_company(new_company)
                    if not nc:
                        st.error("Company name cannot be empty.")
                    else:
                        added = upsert_guest_companies([nc])
                        if added:
                            st.success(f"Added '{nc}' to guestlist.")
                        else:
                            st.info(f"'{nc}' already exists in guestlist.")
                        st.rerun()

    company_norm = normalize_company(company)

    st.divider()

    st.subheader("Main Member (Breakfast + Lunch + Gift)")
    main_name = st.text_input("Main member name *", placeholder="Full name")
    main_phone = st.text_input("Main member phone *", placeholder="10 digits (India)")

    st.subheader("Additional Members (Breakfast + Lunch only)")
    num_extras = st.selectbox("How many additional members?", [0, 1, 2], index=0)

    extras = []
    for i in range(num_extras):
        st.markdown(f"**Extra member #{i+1}**")
        n = st.text_input("Name *", key=f"ex_name_{i}")
        p = st.text_input("Phone *", key=f"ex_phone_{i}")
        extras.append((n, p))

    st.divider()

    if not _wa_ready():
        st.warning("WhatsApp API is not configured. Add WHATSAPP_PHONE_NUMBER_ID / WHATSAPP_ACCESS_TOKEN in secrets.toml.")

    if st.button("✅ Submit + Send WhatsApp QRs", type="primary"):
        # validations
        if not company_norm:
            st.error("Company is required. Search and select a company, or add it on the spot.")
            return
        if not main_name.strip() or not main_phone.strip():
            st.error("Main member name and phone are required.")
            return
        for n, p in extras:
            if not n.strip() or not p.strip():
                st.error("All extra member names and phones are required.")
                return

        # enforce guestlist unless allow_spot_company
        if not company_in_guestlist(company_norm):
            if allow_spot_company:
                upsert_guest_companies([company_norm])
                st.warning(f"Company '{company_norm}' was not in guestlist — added on spot.")
            else:
                st.error(f"Company '{company_norm}' not found in guestlist. Enable on-spot add or add in Admin.")
                return

        # Create company & members
        company_id = get_or_create_company(company_norm)

        # MAIN
        main_token = add_member(company_id, main_name, main_phone, "MAIN")
        set_entitlements(main_token, ["BREAKFAST", "LUNCH", "GIFT"])

        # EXTRAS
        extra_records = []
        for n, p in extras:
            t = add_member(company_id, n, p, "EXTRA")
            set_entitlements(t, ["BREAKFAST", "LUNCH"])
            extra_records.append((n, p, t))

        st.success("Registered. Now generating QRs and sending WhatsApp template messages…")

        # If WA not ready, show tokens for manual fallback
        if not _wa_ready():
            st.error("WhatsApp API not configured; cannot send. Registration saved in DB though.")
            st.info(f"MAIN token: {main_token}")
            for n, p, t in extra_records:
                st.info(f"EXTRA {n} token: {t}")
            return

        # MAIN send via template with image header
        try:
            to = normalize_phone_e164_india(main_phone)
            png = make_qr_png_bytes(main_token)
            media_id = wa_upload_media(png)

            resp = wa_send_template_with_image_header(
                to_e164=to,
                template_name=TEMPLATE_MAIN,
                lang_code=TEMPLATE_LANG,
                header_image_media_id=media_id,
                body_params=[main_name, company_norm],   # {{1}}, {{2}}
            )

            st.success(f"WhatsApp template sent to MAIN: {main_name} ({to})")
            # st.json(resp)  # uncomment for debugging
        except Exception as e:
            st.error(f"Failed sending MAIN WhatsApp: {e}")

        # EXTRAS send via template with image header
        for n, p, t in extra_records:
            try:
                to = normalize_phone_e164_india(p)
                png = make_qr_png_bytes(t)
                media_id = wa_upload_media(png)

                resp = wa_send_template_with_image_header(
                    to_e164=to,
                    template_name=TEMPLATE_EXTRA,
                    lang_code=TEMPLATE_LANG,
                    header_image_media_id=media_id,
                    body_params=[n, company_norm],        # {{1}}, {{2}}
                )

                st.success(f"WhatsApp template sent to EXTRA: {n} ({to})")
                # st.json(resp)  # uncomment for debugging
            except Exception as e:
                st.error(f"Failed sending EXTRA WhatsApp to {n}: {e}")

        with st.expander("View members created for this company"):
            rows = list_company_members(company_id)
            for rid, nm, ph, role, tok, created in rows:
                st.write(f"- **{role}** {nm} ({ph}) — token `{tok}`")


def page_scan():
    st.header("📷 Counter Scan (Green = Approved, Red = Not allowed/Used)")
    checkpoint = st.radio("Select counter", CHECKPOINTS, horizontal=True)
    device = st.text_input("Device name (optional)", placeholder="e.g. Breakfast-1")

    st.divider()
    st.subheader("Scan method A: Hardware scanner (recommended)")
    st.caption("Click the input box once; scanner should be in keyboard/HID mode and send Enter after scan.")

    if "scan_buf" not in st.session_state:
        st.session_state.scan_buf = ""

    def on_scanned():
        raw = st.session_state.scan_buf.strip()
        if not raw:
            return
        color, title, subtitle = redeem(raw, checkpoint, device)
        big_box(color, title, subtitle)
        st.session_state.scan_buf = ""

    st.text_input(
        "Scan token here",
        key="scan_buf",
        placeholder="Click here once, then scan…",
        on_change=on_scanned,
    )

    st.subheader("Scan method B: Phone camera (optional)")
    scanned = camera_scan_component()
    if scanned:
        color, title, subtitle = redeem(scanned, checkpoint, device)
        big_box(color, title, subtitle)

    st.subheader("Manual fallback")
    token_manual = st.text_input("Paste token", key="manual_token")
    if st.button("Redeem manual"):
        color, title, subtitle = redeem(token_manual, checkpoint, device)
        big_box(color, title, subtitle)


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
    col4.metric("Redemptions", r)

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

    st.subheader("WhatsApp API status")
    st.write(f"PHONE_NUMBER_ID present: **{bool(PHONE_NUMBER_ID)}**")
    st.write(f"ACCESS_TOKEN present: **{bool(ACCESS_TOKEN)}**")
    st.write(f"GRAPH_API_VERSION: **{GRAPH_API_VERSION}**")
    st.write(f"MAIN template: **{TEMPLATE_MAIN}**")
    st.write(f"EXTRA template: **{TEMPLATE_EXTRA}**")


# ----------------------------
# Main
# ----------------------------
def main():
    init_db()

    inserted = 0
    try:
        inserted = ensure_guestlist_loaded_once()
    except Exception as e:
        st.sidebar.error(f"Guestlist auto-load failed: {e}")

    st.sidebar.title("Vidira Event System")
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