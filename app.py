"""
InvoiceIQ - Full SaaS Backend
Login + Usage Limits + Razorpay Payment
"""

import os, re, json, time, uuid, io
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, send_file, session, redirect, url_for
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import pandas as pd

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = "/tmp/invoiceiq_uploads"
OUTPUT_FOLDER = "/tmp/invoiceiq_outputs"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "invoiceiq-secret-2024-change-this")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", f"sqlite:///{BASE_DIR}/invoiceiq.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

CORS(app, supports_credentials=True)
db = SQLAlchemy(app)

# ── Config ─────────────────────────────────────────────────────────────────
FREE_INVOICE_LIMIT  = 5
PAID_PRICE_DISPLAY  = "₹499/month"
RAZORPAY_LINK       = os.environ.get("RAZORPAY_LINK", "https://razorpay.me/your-payment-link")
ALLOWED_EXTENSIONS  = {"jpg", "jpeg", "png", "csv"}

# ── Models ─────────────────────────────────────────────────────────────────
class User(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    name          = db.Column(db.String(100), nullable=False)
    is_paid       = db.Column(db.Boolean, default=False)
    invoices_used = db.Column(db.Integer, default=0)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, pw):   self.password_hash = generate_password_hash(pw)
    def check_password(self, pw): return check_password_hash(self.password_hash, pw)
    def to_dict(self):
        return {
            "id": self.id, "email": self.email, "name": self.name,
            "is_paid": self.is_paid, "invoices_used": self.invoices_used,
            "invoices_remaining": None if self.is_paid else max(0, FREE_INVOICE_LIMIT - self.invoices_used),
        }

with app.app_context():
    db.create_all()

# ── Auth helpers ───────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Login required", "code": "AUTH_REQUIRED"}), 401
        return f(*args, **kwargs)
    return decorated

def get_current_user():
    if "user_id" not in session:
        return None
    return User.query.get(session["user_id"])

# ── File helpers ───────────────────────────────────────────────────────────
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# ── Module 1: OCR ──────────────────────────────────────────────────────────
def extract_text_from_image(filepath):
    try:
        import pytesseract
        from PIL import Image
        return pytesseract.image_to_string(Image.open(filepath).convert("RGB"))
    except Exception:
        return None

# ── Module 2: Parse OCR text ───────────────────────────────────────────────
def parse_invoice_from_text(text):
    data = {k: None for k in ["invoice_number","date","vendor_name","total_amount","gst_number"]}
    m = re.search(r"(?:invoice\s*(?:no|number|#)[:\s#]*)([\w\-/]+)", text, re.IGNORECASE)
    if m: data["invoice_number"] = m.group(1).strip()
    else:
        m2 = re.search(r"\b(INV[-/]?\d+)\b", text, re.IGNORECASE)
        if m2: data["invoice_number"] = m2.group(1).strip()
    m = re.search(r"\b(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\d{4}[\/\-]\d{2}[\/\-]\d{2}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})\b", text, re.IGNORECASE)
    if m: data["date"] = m.group(1).strip()
    m = re.search(r"(?:from|vendor|bill\s*from|seller)[:\s]+([A-Za-z0-9 &.,'\-]+)", text, re.IGNORECASE)
    if m: data["vendor_name"] = m.group(1).strip()[:60]
    m = re.search(r"(?:grand\s*total|total\s*amount|total|amount\s*due)[:\s\u20b9$\u20ac\xa3]*([0-9,]+\.?\d*)", text, re.IGNORECASE)
    if m: data["total_amount"] = m.group(1).replace(",","").strip()
    m = re.search(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d])\b", text, re.IGNORECASE)
    if m: data["gst_number"] = m.group(1).upper()
    return data

# ── Module 3: Parse CSV ────────────────────────────────────────────────────
def parse_invoice_from_csv(filepath):
    invoices = []
    ALIASES = {
        "invoice_number": ["invoice_number","invoice no","invoice#","inv no","inv_no","invoice id","bill no"],
        "date":           ["date","invoice_date","bill date","billing date","invoice date"],
        "vendor_name":    ["vendor_name","vendor","seller","company","from","supplier","party name"],
        "total_amount":   ["total_amount","total","amount","grand total","amount due","net amount","payable"],
        "gst_number":     ["gst_number","gst no","gst","gstin","tax id","gst number"],
    }
    try:
        df = pd.read_csv(filepath, dtype=str)
        df.columns = [c.strip().lower() for c in df.columns]
        col_map = {}
        for field, aliases in ALIASES.items():
            for alias in aliases:
                for col in df.columns:
                    if alias in col or col in alias:
                        if col not in col_map: col_map[col] = field
                        break
        for _, row in df.iterrows():
            inv = {k: None for k in ["invoice_number","date","vendor_name","total_amount","gst_number"]}
            for col, field in col_map.items():
                val = row.get(col)
                if pd.notna(val) and str(val).strip(): inv[field] = str(val).strip()
            invoices.append(inv)
    except Exception: pass
    return invoices

# ── Module 4: Validate ─────────────────────────────────────────────────────
def validate_invoice(inv, seen):
    warnings = []
    for f in ["invoice_number","date","vendor_name","total_amount"]:
        if not inv.get(f): warnings.append(f"Missing: {f.replace('_',' ').title()}")
    inv_no = inv.get("invoice_number")
    if inv_no:
        if inv_no in seen: warnings.append(f"Duplicate invoice number: {inv_no}")
        else: seen.add(inv_no)
    return warnings

# ── Module 5: Excel export ─────────────────────────────────────────────────
def export_to_excel(items, output_path):
    rows = []
    for item in items:
        inv = item.get("data") or {}
        rows.append({
            "File Name":      item.get("filename",""),
            "Invoice Number": inv.get("invoice_number",""),
            "Date":           inv.get("date",""),
            "Vendor Name":    inv.get("vendor_name",""),
            "Total Amount":   inv.get("total_amount",""),
            "GST Number":     inv.get("gst_number",""),
            "Warnings":       "; ".join(item.get("warnings",[])) or "OK",
        })
    df = pd.DataFrame(rows)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Invoices")
        ws = writer.sheets["Invoices"]
        from openpyxl.styles import PatternFill, Font, Alignment
        from openpyxl.utils import get_column_letter
        hf = PatternFill("solid", fgColor="1E3A5F")
        ef = PatternFill("solid", fgColor="F8D7DA")
        for ci in range(1, len(df.columns)+1):
            c = ws.cell(row=1, column=ci)
            c.fill = hf; c.font = Font(bold=True, color="FFFFFF", size=11)
            c.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 25
        for ri, item in enumerate(items, 2):
            for ci in range(1, len(df.columns)+1):
                c = ws.cell(row=ri, column=ci)
                c.alignment = Alignment(vertical="center")
                if item.get("warnings"): c.fill = ef
        for ci, col in enumerate(df.columns, 1):
            vals = [str(ws.cell(row=r, column=ci).value or "") for r in range(1, len(rows)+2)]
            ws.column_dimensions[get_column_letter(ci)].width = min(max((len(v) for v in vals), default=10)+4, 45)
    return output_path

# ════════════════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ════════════════════════════════════════════════════════════════════════════

@app.route("/auth/signup", methods=["POST"])
def signup():
    data = request.get_json()
    name  = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    pw    = data.get("password") or ""
    if not name or not email or not pw:
        return jsonify({"error": "Name, email and password are required"}), 400
    if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        return jsonify({"error": "Invalid email address"}), 400
    if len(pw) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({"error": "An account with this email already exists"}), 409
    user = User(name=name, email=email)
    user.set_password(pw)
    db.session.add(user)
    db.session.commit()
    session["user_id"] = user.id
    return jsonify({"message": "Account created!", "user": user.to_dict()}), 201

@app.route("/auth/login", methods=["POST"])
def login():
    data  = request.get_json()
    email = (data.get("email") or "").strip().lower()
    pw    = data.get("password") or ""
    user  = User.query.filter_by(email=email).first()
    if not user or not user.check_password(pw):
        return jsonify({"error": "Invalid email or password"}), 401
    session["user_id"] = user.id
    return jsonify({"message": "Logged in!", "user": user.to_dict()})

@app.route("/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "Logged out"})

@app.route("/auth/me")
def me():
    user = get_current_user()
    if not user: return jsonify({"error": "Not logged in", "code": "AUTH_REQUIRED"}), 401
    return jsonify({"user": user.to_dict()})

# ════════════════════════════════════════════════════════════════════════════
# PAYMENT ROUTES
# ════════════════════════════════════════════════════════════════════════════

@app.route("/payment/info")
@login_required
def payment_info():
    return jsonify({
        "price":         PAID_PRICE_DISPLAY,
        "payment_link":  RAZORPAY_LINK,
        "free_limit":    FREE_INVOICE_LIMIT,
        "features": [
            "Unlimited invoice processing",
            "Batch upload (JPG, PNG, CSV)",
            "Excel + JSON export",
            "Duplicate & missing field detection",
            "Priority support",
        ]
    })

@app.route("/payment/activate", methods=["POST"])
@login_required
def activate_paid():
    """
    In production: verify Razorpay webhook/payment ID here.
    For now: accept a payment_ref code and activate manually.
    """
    data       = request.get_json()
    payment_ref = data.get("payment_ref", "").strip()
    if not payment_ref:
        return jsonify({"error": "Payment reference required"}), 400
    user = get_current_user()
    user.is_paid = True
    db.session.commit()
    return jsonify({"message": "Account upgraded to Pro!", "user": user.to_dict()})

# ════════════════════════════════════════════════════════════════════════════
# PROCESS ROUTE (protected + usage limited)
# ════════════════════════════════════════════════════════════════════════════

@app.route("/process", methods=["POST"])
@login_required
def process_files():
    user = get_current_user()

    # Count how many invoices are in this upload first
    files = request.files.getlist("files")
    if not files or all(f.filename == "" for f in files):
        return jsonify({"error": "No files selected"}), 400

    # Estimate invoice count (each CSV row = 1, each image = 1)
    # We'll enforce limit after processing
    if not user.is_paid:
        remaining = FREE_INVOICE_LIMIT - user.invoices_used
        if remaining <= 0:
            return jsonify({
                "error": "free_limit_reached",
                "message": f"You've used all {FREE_INVOICE_LIMIT} free invoices. Upgrade to Pro for unlimited processing.",
                "payment_link": RAZORPAY_LINK,
                "price": PAID_PRICE_DISPLAY,
            }), 402

    seen   = set()
    results = []
    start  = time.time()
    invoice_count = 0

    for file in files:
        if not file.filename: continue
        if not allowed_file(file.filename):
            results.append({"filename": file.filename, "status": "error",
                            "error": "Unsupported type. Use JPG, PNG, or CSV.", "data": {}, "warnings": []})
            continue

        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(filepath)
        ext = filename.rsplit(".", 1)[1].lower()

        if ext == "csv":
            parsed_list = parse_invoice_from_csv(filepath)
            if not parsed_list:
                results.append({"filename": filename, "status": "error",
                                "error": "Could not parse CSV.", "data": {}, "warnings": []})
            else:
                for idx, inv in enumerate(parsed_list):
                    # Check limit mid-batch for free users
                    if not user.is_paid and (user.invoices_used + invoice_count) >= FREE_INVOICE_LIMIT:
                        results.append({"filename": f"{filename} (row {idx+1})", "status": "error",
                                        "error": "Free limit reached. Upgrade to process more.",
                                        "data": {}, "warnings": []})
                        continue
                    warnings = validate_invoice(inv, seen)
                    results.append({"filename": f"{filename} (row {idx+1})",
                                    "status": "warning" if warnings else "success",
                                    "data": inv, "warnings": warnings})
                    invoice_count += 1
        else:
            if not user.is_paid and (user.invoices_used + invoice_count) >= FREE_INVOICE_LIMIT:
                results.append({"filename": filename, "status": "error",
                                "error": "Free limit reached. Upgrade to process more.",
                                "data": {}, "warnings": []})
            else:
                raw_text = extract_text_from_image(filepath)
                if raw_text is None:
                    results.append({"filename": filename, "status": "error",
                                    "error": "Image OCR unavailable. Please use CSV format.", "data": {}, "warnings": []})
                else:
                    inv = parse_invoice_from_text(raw_text)
                    warnings = validate_invoice(inv, seen)
                    results.append({"filename": filename,
                                    "status": "warning" if warnings else "success",
                                    "data": inv, "warnings": warnings,
                                    "raw_text_preview": raw_text[:300]})
                    invoice_count += 1
        try: os.remove(filepath)
        except: pass

    # Update usage count
    user.invoices_used += invoice_count
    db.session.commit()

    elapsed    = round(time.time() - start, 2)
    session_id = str(uuid.uuid4())[:8]

    json_path = os.path.join(OUTPUT_FOLDER, f"invoices_{session_id}.json")
    with open(json_path, "w") as f: json.dump(results, f, indent=2)

    excel_path = os.path.join(OUTPUT_FOLDER, f"invoices_{session_id}.xlsx")
    export_to_excel(results, excel_path)

    return jsonify({
        "results":          results,
        "processing_time":  elapsed,
        "session_id":       session_id,
        "invoices_used":    user.invoices_used,
        "is_paid":          user.is_paid,
        "invoices_remaining": None if user.is_paid else max(0, FREE_INVOICE_LIMIT - user.invoices_used),
        "summary": {
            "success":  sum(1 for r in results if r["status"] == "success"),
            "warnings": sum(1 for r in results if r["status"] == "warning"),
            "errors":   sum(1 for r in results if r["status"] == "error"),
        },
    })

# ════════════════════════════════════════════════════════════════════════════
# DOWNLOAD ROUTES
# ════════════════════════════════════════════════════════════════════════════

@app.route("/download/excel/<session_id>")
@login_required
def download_excel(session_id):
    if not re.match(r'^[a-f0-9]{8}$', session_id):
        return jsonify({"error": "Invalid session"}), 400
    path = os.path.join(OUTPUT_FOLDER, f"invoices_{session_id}.xlsx")
    if not os.path.exists(path): return jsonify({"error": "File not found"}), 404
    return send_file(path, as_attachment=True, download_name="invoices.xlsx")

@app.route("/download/json/<session_id>")
@login_required
def download_json(session_id):
    if not re.match(r'^[a-f0-9]{8}$', session_id):
        return jsonify({"error": "Invalid session"}), 400
    path = os.path.join(OUTPUT_FOLDER, f"invoices_{session_id}.json")
    if not os.path.exists(path): return jsonify({"error": "File not found"}), 404
    return send_file(path, as_attachment=True, download_name="invoices.json")

# ════════════════════════════════════════════════════════════════════════════
# STATIC + HEALTH
# ════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_file(os.path.join(BASE_DIR, "index.html"))

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 InvoiceIQ running on http://localhost:{port}")
    app.run(debug=False, host="0.0.0.0", port=port)
