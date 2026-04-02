from flask import Flask, request, redirect, url_for, render_template_string, flash, session
import sqlite3
import os
import secrets
from datetime import datetime, date, timedelta
from functools import wraps

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-this-secret-key")
DB_PATH = "moy_servis_licensed.db"
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "12345")

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key TEXT NOT NULL UNIQUE,
            branch_name TEXT NOT NULL,
            owner_name TEXT,
            phone_number TEXT,
            duration_months INTEGER NOT NULL,
            activated INTEGER NOT NULL DEFAULT 0,
            activated_at TEXT,
            expires_at TEXT,
            device_name TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            notes TEXT,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            branch_name TEXT NOT NULL,
            car_brand TEXT NOT NULL,
            client_name TEXT NOT NULL,
            oil_name TEXT NOT NULL,
            current_km INTEGER NOT NULL,
            last_changed_date TEXT NOT NULL,
            next_km INTEGER,
            next_date TEXT,
            phone_number TEXT NOT NULL,
            sms_enabled INTEGER NOT NULL DEFAULT 1,
            notes TEXT,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sms_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def parse_date(date_str):
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            pass
    return None

def parse_dt(dt_str):
    if not dt_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(dt_str, fmt)
        except ValueError:
            pass
    return None

def add_months(start_date, months):
    return start_date + timedelta(days=30 * months)

def generate_license_key():
    return f"MOS-{secrets.token_hex(3).upper()}-{secrets.token_hex(3).upper()}-{secrets.token_hex(2).upper()}"

def setting_get(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default

def setting_set(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value)
    )
    conn.commit()
    conn.close()

def get_active_license():
    license_key = setting_get("active_license_key")
    if not license_key:
        return None
    conn = get_conn()
    row = conn.execute("SELECT * FROM licenses WHERE license_key = ?", (license_key,)).fetchone()
    conn.close()
    return row

def license_status():
    row = get_active_license()
    if not row:
        return "not_activated", None
    if row["is_active"] != 1:
        return "disabled", row
    if row["activated"] != 1:
        return "not_activated", row
    expires_dt = parse_dt(row["expires_at"])
    if not expires_dt:
        return "invalid", row
    if expires_dt.date() < date.today():
        return "expired", row
    return "ok", row

def activate_license(license_key, device_name="Main-PC"):
    conn = get_conn()
    row = conn.execute("SELECT * FROM licenses WHERE license_key = ?", (license_key,)).fetchone()
    if not row:
        conn.close()
        return False, "Litsenziya topilmadi"
    if row["is_active"] != 1:
        conn.close()
        return False, "Litsenziya o'chirilgan"
    if row["activated"] == 1:
        setting_set("active_license_key", license_key)
        conn.close()
        return True, "Litsenziya avval aktivatsiya qilingan va tizimga ulandi"

    start_dt = datetime.now()
    expires_dt = add_months(start_dt, int(row["duration_months"]))
    conn.execute(
        """
        UPDATE licenses
        SET activated = 1,
            activated_at = ?,
            expires_at = ?,
            device_name = ?
        WHERE license_key = ?
        """,
        (start_dt.strftime("%Y-%m-%d %H:%M:%S"), expires_dt.strftime("%Y-%m-%d %H:%M:%S"), device_name, license_key)
    )
    conn.commit()
    conn.close()
    setting_set("active_license_key", license_key)
    return True, "Litsenziya muvaffaqiyatli aktivatsiya qilindi"

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper

def app_license_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        status, _ = license_status()
        if status != "ok":
            return redirect(url_for("license_page"))
        return f(*args, **kwargs)
    return wrapper

def normalize_phone(phone):
    return (phone or "").replace(" ", "").strip()

def get_record_status(record):
    next_date = parse_date(record["next_date"]) if record["next_date"] else None
    if next_date:
        if next_date < date.today():
            return "overdue"
        if next_date == date.today():
            return "due"
    return "ok"

def create_sms_text(record):
    return (
        f"Assalomu alaykum, {record['client_name']}. "
        f"{record['car_brand']} mashinangiz uchun {record['oil_name']} moyini almashtirish vaqti keldi. "
        f"Shoxobcha: {record['branch_name']}. "
        f"Telefon: {record['phone_number']}"
    )

def send_sms(phone_number, message):
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_FROM_NUMBER")

    if account_sid and auth_token and from_number:
        try:
            from twilio.rest import Client
            client = Client(account_sid, auth_token)
            client.messages.create(body=message, from_=from_number, to=phone_number)
            status = "sent"
            ok = True
            result = "SMS yuborildi"
        except Exception as e:
            status = "error"
            ok = False
            result = f"SMS xatolik: {e}"
    else:
        print("[SMS TEST MODE]")
        print("TO:", phone_number)
        print("TEXT:", message)
        status = "test_mode"
        ok = True
        result = "Twilio sozlanmagan. SMS test rejimida konsolga chiqarildi"

    conn = get_conn()
    conn.execute(
        "INSERT INTO sms_logs (phone_number, message, status, created_at) VALUES (?, ?, ?, ?)",
        (phone_number, message, status, now_str())
    )
    conn.commit()
    conn.close()
    return ok, result

@app.route("/license", methods=["GET", "POST"])
def license_page():
    status, active = license_status()
    if request.method == "POST":
        license_key = request.form.get("license_key", "").strip().upper()
        device_name = request.form.get("device_name", "Main-PC").strip() or "Main-PC"
        ok, msg = activate_license(license_key, device_name)
        flash(msg, "success" if ok else "error")
        return redirect(url_for("license_page"))
    return render_template_string(LICENSE_TEMPLATE, status=status, active=active)

@app.route("/")
@app_license_required
def index():
    q = request.args.get("q", "").strip().lower()
    status_filter = request.args.get("status", "all")
    active = get_active_license()
    active_branch = active["branch_name"] if active else ""

    conn = get_conn()
    rows = conn.execute("SELECT * FROM records WHERE branch_name = ? ORDER BY id DESC", (active_branch,)).fetchall()
    conn.close()

    filtered = []
    for row in rows:
        row_dict = dict(row)
        row_dict["status"] = get_record_status(row)
        haystack = " ".join([
            row_dict["branch_name"], row_dict["car_brand"], row_dict["client_name"],
            row_dict["oil_name"], row_dict["phone_number"], row_dict.get("notes") or ""
        ]).lower()
        if q and q not in haystack:
            continue
        if status_filter != "all" and row_dict["status"] != status_filter:
            continue
        filtered.append(row_dict)

    today_count = sum(1 for r in filtered if r["status"] == "due")
    overdue_count = sum(1 for r in filtered if r["status"] == "overdue")
    sms_ready_count = sum(1 for r in filtered if r["sms_enabled"] == 1 and r["status"] in ("due", "overdue"))

    return render_template_string(
        APP_TEMPLATE,
        records=filtered,
        q=q,
        status_filter=status_filter,
        total_count=len(filtered),
        today_count=today_count,
        overdue_count=overdue_count,
        sms_ready_count=sms_ready_count,
        active=active
    )

@app.route("/add", methods=["POST"])
@app_license_required
def add_record():
    active = get_active_license()
    branch_name = active["branch_name"] if active else request.form.get("branch_name", "").strip()
    car_brand = request.form.get("car_brand", "").strip()
    client_name = request.form.get("client_name", "").strip()
    oil_name = request.form.get("oil_name", "").strip()
    current_km = request.form.get("current_km", "").strip()
    last_changed_date = request.form.get("last_changed_date", "").strip()
    next_km = request.form.get("next_km", "").strip()
    next_date = request.form.get("next_date", "").strip()
    phone_number = normalize_phone(request.form.get("phone_number", ""))
    sms_enabled = 1 if request.form.get("sms_enabled") == "on" else 0
    notes = request.form.get("notes", "").strip()

    if not all([branch_name, car_brand, client_name, oil_name, current_km, last_changed_date, phone_number]):
        flash("Majburiy maydonlarni to'ldiring", "error")
        return redirect(url_for("index"))

    conn = get_conn()
    conn.execute(
        """
        INSERT INTO records (
            branch_name, car_brand, client_name, oil_name,
            current_km, last_changed_date, next_km, next_date,
            phone_number, sms_enabled, notes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            branch_name, car_brand, client_name, oil_name,
            int(current_km), last_changed_date,
            int(next_km) if next_km else None,
            next_date or None, phone_number,
            sms_enabled, notes, now_str()
        )
    )
    conn.commit()
    conn.close()
    flash("Yozuv saqlandi", "success")
    return redirect(url_for("index"))

@app.route("/delete/<int:record_id>", methods=["POST"])
@app_license_required
def delete_record(record_id):
    conn = get_conn()
    conn.execute("DELETE FROM records WHERE id = ?", (record_id,))
    conn.commit()
    conn.close()
    flash("Yozuv o'chirildi", "success")
    return redirect(url_for("index"))

@app.route("/sms/<int:record_id>", methods=["POST"])
@app_license_required
def sms_record(record_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
    conn.close()
    if not row:
        flash("Yozuv topilmadi", "error")
        return redirect(url_for("index"))
    message = create_sms_text(row)
    ok, result = send_sms(row["phone_number"], message)
    flash(result, "success" if ok else "error")
    return redirect(url_for("index"))

@app.route("/send-due-sms", methods=["POST"])
@app_license_required
def send_due_sms():
    active = get_active_license()
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM records WHERE sms_enabled = 1 AND branch_name = ?",
        (active["branch_name"],)
    ).fetchall()
    conn.close()

    sent = 0
    for row in rows:
        status = get_record_status(row)
        if status in ("due", "overdue"):
            message = create_sms_text(row)
            ok, _ = send_sms(row["phone_number"], message)
            if ok:
                sent += 1

    flash(f"{sent} ta SMS yuborildi yoki test rejimida chiqarildi", "success")
    return redirect(url_for("index"))

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            return redirect(url_for("admin_panel"))
        flash("Login yoki parol xato", "error")
    return render_template_string(ADMIN_LOGIN_TEMPLATE)

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    return redirect(url_for("admin_login"))

@app.route("/admin")
@admin_required
def admin_panel():
    conn = get_conn()
    licenses = conn.execute("SELECT * FROM licenses ORDER BY id DESC").fetchall()
    sms_logs = conn.execute("SELECT * FROM sms_logs ORDER BY id DESC LIMIT 20").fetchall()
    conn.close()
    return render_template_string(ADMIN_TEMPLATE, licenses=licenses, sms_logs=sms_logs)

@app.route("/admin/create-license", methods=["POST"])
@admin_required
def create_license():
    branch_name = request.form.get("branch_name", "").strip()
    owner_name = request.form.get("owner_name", "").strip()
    phone_number = normalize_phone(request.form.get("phone_number", ""))
    duration_months = int(request.form.get("duration_months", "12"))
    notes = request.form.get("notes", "").strip()

    if not branch_name:
        flash("Shoxobcha nomi kerak", "error")
        return redirect(url_for("admin_panel"))

    license_key = generate_license_key()
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO licenses (
            license_key, branch_name, owner_name, phone_number,
            duration_months, notes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (license_key, branch_name, owner_name, phone_number, duration_months, notes, now_str())
    )
    conn.commit()
    conn.close()
    flash(f"Yangi litsenziya yaratildi: {license_key}", "success")
    return redirect(url_for("admin_panel"))

@app.route("/admin/toggle-license/<int:license_id>", methods=["POST"])
@admin_required
def toggle_license(license_id):
    conn = get_conn()
    row = conn.execute("SELECT is_active FROM licenses WHERE id = ?", (license_id,)).fetchone()
    if row:
        new_value = 0 if row["is_active"] == 1 else 1
        conn.execute("UPDATE licenses SET is_active = ? WHERE id = ?", (new_value, license_id))
        conn.commit()
    conn.close()
    flash("Litsenziya holati o'zgartirildi", "success")
    return redirect(url_for("admin_panel"))

@app.route("/admin/extend-license/<int:license_id>", methods=["POST"])
@admin_required
def extend_license(license_id):
    months = int(request.form.get("months", "1"))
    conn = get_conn()
    row = conn.execute("SELECT * FROM licenses WHERE id = ?", (license_id,)).fetchone()
    if not row:
        conn.close()
        flash("Litsenziya topilmadi", "error")
        return redirect(url_for("admin_panel"))

    base_dt = parse_dt(row["expires_at"]) if row["expires_at"] else datetime.now()
    if base_dt.date() < date.today():
        base_dt = datetime.now()
    new_expiry = add_months(base_dt, months)
    conn.execute(
        "UPDATE licenses SET expires_at = ?, activated = 1 WHERE id = ?",
        (new_expiry.strftime("%Y-%m-%d %H:%M:%S"), license_id)
    )
    conn.commit()
    conn.close()
    flash(f"Litsenziya {months} oyga uzaytirildi", "success")
    return redirect(url_for("admin_panel"))

LICENSE_TEMPLATE = """
<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Litsenziya aktivatsiyasi</title>
<style>
body{font-family:Arial,sans-serif;background:#f4f7fb;margin:0;color:#1f2937}
.box{max-width:700px;margin:40px auto;background:white;padding:24px;border-radius:18px;box-shadow:0 8px 24px rgba(0,0,0,.06)}
input,button{width:100%;padding:12px;border-radius:10px;border:1px solid #d1d5db;box-sizing:border-box;margin-top:10px}
button{background:#2563eb;color:white;border:none;font-weight:bold;cursor:pointer}
.alert{padding:12px;border-radius:12px;margin:12px 0}
.ok{background:#ecfdf5;color:#065f46}.bad{background:#fef2f2;color:#991b1b}.warn{background:#fff7ed;color:#9a3412}
small{color:#6b7280}
</style>
</head>
<body>
<div class="box">
<h1>Litsenziya aktivatsiyasi</h1>
{% with messages = get_flashed_messages(with_categories=true) %}
  {% if messages %}
    {% for category, message in messages %}
      <div class="alert {{ 'ok' if category == 'success' else 'bad' }}">{{ message }}</div>
    {% endfor %}
  {% endif %}
{% endwith %}

{% if status == 'ok' and active %}
  <div class="alert ok">
    <b>Faol litsenziya bor</b><br>
    Shoxobcha: {{ active['branch_name'] }}<br>
    Kalit: {{ active['license_key'] }}<br>
    Tugash sanasi: {{ active['expires_at'] }}
  </div>
  <a href="/"><button>Dasturga kirish</button></a>
{% else %}
  <div class="alert {{ 'warn' if status in ['expired','disabled'] else 'bad' }}">
    {% if status == 'expired' %}
      Litsenziya muddati tugagan.
    {% elif status == 'disabled' %}
      Litsenziya admin tomonidan o'chirilgan.
    {% else %}
      Dastur hali aktivatsiya qilinmagan.
    {% endif %}
  </div>
  <form method="post">
    <input name="license_key" placeholder="Litsenziya kalitini kiriting" required>
    <input name="device_name" placeholder="Kompyuter nomi" value="Main-PC">
    <button type="submit">Aktivatsiya qilish</button>
  </form>
{% endif %}
<p><small>Admin panel: /admin/login</small></p>
</div>
</body>
</html>
"""

APP_TEMPLATE = """
<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Moy servis</title>
<style>
body{font-family:Arial,sans-serif;background:#f4f7fb;margin:0;color:#1f2937}
header{background:linear-gradient(135deg,#0f172a,#2563eb);color:white;padding:24px;text-align:center}
.container{max-width:1400px;margin:20px auto;padding:0 16px 32px}
.card{background:white;border-radius:18px;padding:16px;margin-bottom:16px;box-shadow:0 8px 24px rgba(0,0,0,.06)}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.stat{background:#eff6ff;border:1px solid #dbeafe;border-radius:14px;padding:14px}
.stat small{display:block;color:#6b7280;margin-bottom:6px}.stat strong{font-size:24px}
input,textarea,select,button{width:100%;padding:11px 12px;border-radius:10px;border:1px solid #d1d5db;font-size:14px;box-sizing:border-box}
textarea{min-height:72px;resize:vertical}button{background:#2563eb;color:white;border:none;font-weight:bold;cursor:pointer}
button.green{background:#059669}button.red{background:#dc2626}button.gray{background:#4b5563}button.orange{background:#d97706}
table{width:100%;border-collapse:collapse;margin-top:12px}th,td{padding:10px;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top;font-size:14px}
.badge{display:inline-block;padding:5px 9px;border-radius:999px;color:white;font-size:12px;font-weight:bold}.ok{background:#059669}.due{background:#d97706}.overdue{background:#dc2626}
.flash{padding:12px;border-radius:10px;margin-bottom:12px}.flash.success{background:#ecfdf5;color:#065f46}.flash.error{background:#fef2f2;color:#991b1b}
.actions{display:flex;gap:6px;flex-wrap:wrap}.muted{color:#6b7280;font-size:12px}.topline{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}
@media (max-width:1100px){.grid,.stats{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <h1>Moy Almashtirish Shoxobchasi</h1>
  <p>{{ active['branch_name'] }} uchun litsenziyalangan tizim</p>
</header>
<div class="container">
{% with messages = get_flashed_messages(with_categories=true) %}
  {% if messages %}
    {% for category, message in messages %}
      <div class="flash {{ category }}">{{ message }}</div>
    {% endfor %}
  {% endif %}
{% endwith %}

<div class="card topline">
  <div>
    <b>Litsenziya:</b> {{ active['license_key'] }}<br>
    <span class="muted">Tugash sanasi: {{ active['expires_at'] }}</span>
  </div>
  <div>
    <a href="/license"><button class="gray">Litsenziya holati</button></a>
  </div>
</div>

<div class="card">
  <div class="stats">
    <div class="stat"><small>Jami yozuv</small><strong>{{ total_count }}</strong></div>
    <div class="stat"><small>Bugun vaqti kelgan</small><strong>{{ today_count }}</strong></div>
    <div class="stat"><small>Kechikkan</small><strong>{{ overdue_count }}</strong></div>
    <div class="stat"><small>SMS tayyor</small><strong>{{ sms_ready_count }}</strong></div>
  </div>
</div>

<div class="card">
  <h2>Yangi yozuv</h2>
  <form method="post" action="{{ url_for('add_record') }}">
    <div class="grid">
      <input value="{{ active['branch_name'] }}" disabled>
      <input name="car_brand" placeholder="Mashina markasi" required>
      <input name="client_name" placeholder="Klient nomi" required>
      <input name="phone_number" placeholder="Telefon nomeri" required>

      <input name="oil_name" placeholder="Moy nomi" required>
      <input name="current_km" type="number" placeholder="Yurgan kilometri" required>
      <input name="last_changed_date" type="date" required>
      <input name="next_km" type="number" placeholder="Keyingi kilometr">

      <input name="next_date" type="date">
      <label style="display:flex;align-items:center;gap:8px;padding:10px 0;">
        <input type="checkbox" name="sms_enabled" checked style="width:auto;"> SMS yuborilsin
      </label>
      <textarea name="notes" placeholder="Qo'shimcha izoh"></textarea>
      <button class="green" type="submit">Saqlash</button>
    </div>
  </form>
</div>

<div class="card">
  <h2>Qidiruv va filtr</h2>
  <form method="get">
    <div class="grid" style="grid-template-columns:2fr 1fr 1fr 1fr;">
      <input name="q" value="{{ q }}" placeholder="Klient, telefon, mashina...">
      <select name="status">
        <option value="all" {% if status_filter == 'all' %}selected{% endif %}>Barchasi</option>
        <option value="due" {% if status_filter == 'due' %}selected{% endif %}>Vaqti kelgan</option>
        <option value="overdue" {% if status_filter == 'overdue' %}selected{% endif %}>Kechikkan</option>
        <option value="ok" {% if status_filter == 'ok' %}selected{% endif %}>Yaxshi</option>
      </select>
      <button type="submit">Qidirish</button>
      <button formaction="{{ url_for('send_due_sms') }}" formmethod="post" class="orange" type="submit">Vaqti kelganlarga SMS</button>
    </div>
  </form>
</div>

<div class="card">
  <h2>Mijozlar ro'yxati</h2>
  <table>
    <thead>
      <tr>
        <th>Holat</th><th>Klient</th><th>Mashina</th><th>Moy</th><th>Kilometr</th><th>Sana</th><th>Telefon</th><th>Amal</th>
      </tr>
    </thead>
    <tbody>
      {% for r in records %}
      <tr>
        <td>
          {% if r.status == 'ok' %}<span class="badge ok">Yaxshi</span>
          {% elif r.status == 'due' %}<span class="badge due">Vaqti kelgan</span>
          {% else %}<span class="badge overdue">Kechikkan</span>{% endif %}
        </td>
        <td><strong>{{ r.client_name }}</strong><div class="muted">{{ r.notes or '' }}</div></td>
        <td>{{ r.car_brand }}</td>
        <td>{{ r.oil_name }}</td>
        <td>Hozir: {{ r.current_km }} km<br><span class="muted">Keyingi: {{ r.next_km or '-' }} km</span></td>
        <td>Oxirgi: {{ r.last_changed_date }}<br><span class="muted">Keyingi: {{ r.next_date or '-' }}</span></td>
        <td>{{ r.phone_number }}</td>
        <td>
          <div class="actions">
            <form method="post" action="{{ url_for('sms_record', record_id=r.id) }}"><button type="submit">SMS</button></form>
            <form method="post" action="{{ url_for('delete_record', record_id=r.id) }}" onsubmit="return confirm('O\\'chirilsinmi?')"><button class="red" type="submit">O'chirish</button></form>
          </div>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
</div>
</body>
</html>
"""

ADMIN_LOGIN_TEMPLATE = """
<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin login</title>
<style>
body{font-family:Arial,sans-serif;background:#f4f7fb;margin:0;color:#1f2937}.box{max-width:500px;margin:60px auto;background:white;padding:24px;border-radius:18px;box-shadow:0 8px 24px rgba(0,0,0,.06)}
input,button{width:100%;padding:12px;border-radius:10px;border:1px solid #d1d5db;box-sizing:border-box;margin-top:10px}button{background:#2563eb;color:white;border:none;font-weight:bold}
.flash{padding:12px;border-radius:10px;margin-bottom:12px;background:#fef2f2;color:#991b1b}
</style>
</head>
<body>
<div class="box">
  <h1>Admin login</h1>
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
      {% for category, message in messages %}
        <div class="flash">{{ message }}</div>
      {% endfor %}
    {% endif %}
  {% endwith %}
  <form method="post">
    <input name="username" placeholder="Login" required>
    <input name="password" type="password" placeholder="Parol" required>
    <button type="submit">Kirish</button>
  </form>
</div>
</body>
</html>
"""

ADMIN_TEMPLATE = """
<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin panel</title>
<style>
body{font-family:Arial,sans-serif;background:#f4f7fb;margin:0;color:#1f2937}header{background:linear-gradient(135deg,#111827,#2563eb);color:white;padding:24px;text-align:center}
.container{max-width:1400px;margin:20px auto;padding:0 16px 32px}.card{background:white;border-radius:18px;padding:16px;margin-bottom:16px;box-shadow:0 8px 24px rgba(0,0,0,.06)}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}input,textarea,select,button{width:100%;padding:11px 12px;border-radius:10px;border:1px solid #d1d5db;font-size:14px;box-sizing:border-box}textarea{min-height:72px}button{background:#2563eb;color:white;border:none;font-weight:bold;cursor:pointer}
button.green{background:#059669}button.red{background:#dc2626}button.gray{background:#4b5563}button.orange{background:#d97706}table{width:100%;border-collapse:collapse;margin-top:12px}th,td{padding:10px;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top;font-size:14px}
.flash{padding:12px;border-radius:10px;margin-bottom:12px}.flash.success{background:#ecfdf5;color:#065f46}.flash.error{background:#fef2f2;color:#991b1b}.actions{display:flex;gap:6px;flex-wrap:wrap}.muted{color:#6b7280;font-size:12px}
@media (max-width:1100px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <h1>Admin Panel</h1>
  <p>Litsenziya yaratish va boshqarish</p>
</header>
<div class="container">
{% with messages = get_flashed_messages(with_categories=true) %}
  {% if messages %}
    {% for category, message in messages %}
      <div class="flash {{ category }}">{{ message }}</div>
    {% endfor %}
  {% endif %}
{% endwith %}

<div class="card">
  <div style="display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap">
    <h2>Yangi litsenziya</h2>
    <a href="/admin/logout"><button class="gray">Chiqish</button></a>
  </div>
  <form method="post" action="{{ url_for('create_license') }}">
    <div class="grid">
      <input name="branch_name" placeholder="Shoxobcha nomi" required>
      <input name="owner_name" placeholder="Egasining ismi">
      <input name="phone_number" placeholder="Telefon raqami">
      <select name="duration_months">
        <option value="1">1 oy</option>
        <option value="3">3 oy</option>
        <option value="6">6 oy</option>
        <option value="12" selected>12 oy</option>
      </select>
      <textarea name="notes" placeholder="Izoh"></textarea>
      <button class="green" type="submit">Litsenziya yaratish</button>
    </div>
  </form>
</div>

<div class="card">
  <h2>Litsenziyalar</h2>
  <table>
    <thead>
      <tr>
        <th>ID</th><th>Kalit</th><th>Shoxobcha</th><th>Telefon</th><th>Muddat</th><th>Aktiv</th><th>Tugash</th><th>Qurilma</th><th>Amal</th>
      </tr>
    </thead>
    <tbody>
      {% for l in licenses %}
      <tr>
        <td>{{ l.id }}</td>
        <td><b>{{ l.license_key }}</b><div class="muted">{{ l.created_at }}</div></td>
        <td>{{ l.branch_name }}<div class="muted">{{ l.owner_name or '' }}</div></td>
        <td>{{ l.phone_number or '-' }}</td>
        <td>{{ l.duration_months }} oy</td>
        <td>{{ 'Ha' if l.is_active == 1 else 'Yo\\'q' }}</td>
        <td>{{ l.expires_at or '-' }}</td>
        <td>{{ l.device_name or '-' }}</td>
        <td>
          <div class="actions">
            <form method="post" action="{{ url_for('toggle_license', license_id=l.id) }}">
              <button class="{{ 'red' if l.is_active == 1 else 'green' }}" type="submit">{{ 'O\\'chirish' if l.is_active == 1 else 'Yoqish' }}</button>
            </form>
            <form method="post" action="{{ url_for('extend_license', license_id=l.id) }}">
              <input type="hidden" name="months" value="1">
              <button class="orange" type="submit">+1 oy</button>
            </form>
            <form method="post" action="{{ url_for('extend_license', license_id=l.id) }}">
              <input type="hidden" name="months" value="12">
              <button class="gray" type="submit">+12 oy</button>
            </form>
          </div>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>

<div class="card">
  <h2>Oxirgi SMS loglar</h2>
  <table>
    <thead><tr><th>Vaqt</th><th>Telefon</th><th>Status</th><th>Xabar</th></tr></thead>
    <tbody>
      {% for s in sms_logs %}
      <tr>
        <td>{{ s.created_at }}</td>
        <td>{{ s.phone_number }}</td>
        <td>{{ s.status }}</td>
        <td>{{ s.message }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
</div>
</body>
</html>
"""
init_db()
if __name__ == "__main__":
      app.run()
