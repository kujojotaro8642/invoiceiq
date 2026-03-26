"""
Invoice Automation Tool - Flask Backend
Beginner-friendly, modular design
Cloud-ready: works on Render, Railway, and locally
"""

import os
import re
import json
import time
import uuid
import io
from datetime import datetime
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import pandas as pd
from werkzeug.utils import secure_filename

# BASE_DIR = folder where app.py lives (works everywhere)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="")
CORS(app)

# Use /tmp on cloud (Render/Railway), safe on local too
UPLOAD_FOLDER = "/tmp/invoiceiq_uploads"
OUTPUT_FOLDER = "/tmp/invoiceiq_outputs"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "csv"}
seen_invoice_numbers = set()


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ── Module 1: OCR ──────────────────────────────────────────────────────────
def extract_text_from_image(filepath):
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(filepath).convert("RGB")
        return pytesseract.image_to_string(img)
    except ImportError:
        return "[OCR unavailable: tesseract not installed on this server]"
    except Exception as e:
        return f"[OCR error: {str(e)}]"


# ── Module 2: Parse text from OCR ──────────────────────────────────────────
def parse_invoice_from_text(text):
    data = {
        "invoice_number": None,
        "date": None,
        "vendor_name": None,
        "total_amount": None,
        "gst_number": None,
    }

    inv_match = re.search(r"(?:invoice\s*(?:no|number|#)[:\s#]*)([\w\-/]+)", text, re.IGNORECASE)
    if inv_match:
        data["invoice_number"] = inv_match.group(1).strip()
    else:
        m2 = re.search(r"\b(INV[-/]?\d+)\b", text, re.IGNORECASE)
        if m2:
            data["invoice_number"] = m2.group(1).strip()

    date_match = re.search(
        r"\b(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\d{4}[\/\-]\d{2}[\/\-]\d{2}|"
        r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})\b",
        text, re.IGNORECASE
    )
    if date_match:
        data["date"] = date_match.group(1).strip()

    vendor_match = re.search(
        r"(?:from|vendor|bill\s*from|seller)[:\s]+([A-Za-z0-9 &.,'\-]+)", text, re.IGNORECASE
    )
    if vendor_match:
        data["vendor_name"] = vendor_match.group(1).strip()[:60]

    total_match = re.search(
        r"(?:grand\s*total|total\s*amount|total|amount\s*due)[:\s\u20b9$\u20ac\xa3]*([0-9,]+\.?\d*)",
        text, re.IGNORECASE
    )
    if total_match:
        data["total_amount"] = total_match.group(1).replace(",", "").strip()

    gst_match = re.search(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d])\b", text, re.IGNORECASE)
    if gst_match:
        data["gst_number"] = gst_match.group(1).upper()

    return data


# ── Module 3: Parse CSV ────────────────────────────────────────────────────
def parse_invoice_from_csv(filepath):
    invoices = []
    COLUMN_ALIASES = {
        "invoice_number": ["invoice_number", "invoice no", "invoice#", "inv no", "inv_no", "invoice id", "bill no"],
        "date":           ["date", "invoice_date", "bill date", "billing date", "invoice date"],
        "vendor_name":    ["vendor_name", "vendor", "seller", "company", "from", "supplier", "party name"],
        "total_amount":   ["total_amount", "total", "amount", "grand total", "amount due", "net amount", "payable"],
        "gst_number":     ["gst_number", "gst no", "gst", "gstin", "tax id", "gst number"],
    }
    try:
        df = pd.read_csv(filepath, dtype=str)
        df.columns = [c.strip().lower() for c in df.columns]
        col_mapping = {}
        for field, aliases in COLUMN_ALIASES.items():
            for alias in aliases:
                for col in df.columns:
                    if alias in col or col in alias:
                        if col not in col_mapping:
                            col_mapping[col] = field
                        break
        for _, row in df.iterrows():
            inv = {k: None for k in ["invoice_number", "date", "vendor_name", "total_amount", "gst_number"]}
            for col, field in col_mapping.items():
                val = row.get(col)
                if pd.notna(val) and str(val).strip():
                    inv[field] = str(val).strip()
            invoices.append(inv)
    except Exception:
        pass
    return invoices


# ── Module 4: Validate ─────────────────────────────────────────────────────
def validate_invoice(inv, seen_numbers):
    warnings = []
    for field in ["invoice_number", "date", "vendor_name", "total_amount"]:
        if not inv.get(field):
            warnings.append(f"Missing: {field.replace('_', ' ').title()}")
    inv_no = inv.get("invoice_number")
    if inv_no:
        if inv_no in seen_numbers:
            warnings.append(f"Duplicate invoice number: {inv_no}")
        else:
            seen_numbers.add(inv_no)
    return warnings


# ── Module 5: Export Excel ─────────────────────────────────────────────────
def export_to_excel(invoices_with_meta, output_path):
    rows = []
    for item in invoices_with_meta:
        inv = item.get("data") or {}
        rows.append({
            "File Name":      item.get("filename", ""),
            "Invoice Number": inv.get("invoice_number", ""),
            "Date":           inv.get("date", ""),
            "Vendor Name":    inv.get("vendor_name", ""),
            "Total Amount":   inv.get("total_amount", ""),
            "GST Number":     inv.get("gst_number", ""),
            "Warnings":       "; ".join(item.get("warnings", [])) or "OK",
        })
    df = pd.DataFrame(rows)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Invoices")
        ws = writer.sheets["Invoices"]
        from openpyxl.styles import PatternFill, Font, Alignment
        from openpyxl.utils import get_column_letter
        header_fill = PatternFill("solid", fgColor="1E3A5F")
        header_font = Font(bold=True, color="FFFFFF", size=11)
        err_fill    = PatternFill("solid", fgColor="F8D7DA")
        for col_idx in range(1, len(df.columns) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 25
        for row_idx, item in enumerate(invoices_with_meta, 2):
            for col_idx in range(1, len(df.columns) + 1):
                cell = ws.cell(row=row_idx, column=col_idx)
                cell.alignment = Alignment(vertical="center")
                if item.get("warnings"):
                    cell.fill = err_fill
        for col_idx, col in enumerate(df.columns, 1):
            values = [str(ws.cell(row=r, column=col_idx).value or "") for r in range(1, len(rows) + 2)]
            ws.column_dimensions[get_column_letter(col_idx)].width = min(max((len(v) for v in values), default=10) + 4, 45)
    return output_path


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(os.path.join(BASE_DIR, "index.html"))


@app.route("/process", methods=["POST"])
def process_files():
    global seen_invoice_numbers
    seen_invoice_numbers = set()

    if "files" not in request.files:
        return jsonify({"error": "No files uploaded"}), 400

    files = request.files.getlist("files")
    if not files or all(f.filename == "" for f in files):
        return jsonify({"error": "No files selected"}), 400

    results = []
    start_time = time.time()

    for file in files:
        if not file.filename:
            continue
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
                                "error": "Could not parse CSV. Check column names.", "data": {}, "warnings": []})
            else:
                for idx, inv in enumerate(parsed_list):
                    warnings = validate_invoice(inv, seen_invoice_numbers)
                    results.append({"filename": f"{filename} (row {idx+1})",
                                    "status": "warning" if warnings else "success",
                                    "data": inv, "warnings": warnings})
        else:
            raw_text = extract_text_from_image(filepath)
            inv = parse_invoice_from_text(raw_text)
            warnings = validate_invoice(inv, seen_invoice_numbers)
            results.append({"filename": filename,
                            "status": "warning" if warnings else "success",
                            "data": inv, "warnings": warnings,
                            "raw_text_preview": raw_text[:300]})

        try:
            os.remove(filepath)
        except Exception:
            pass

    elapsed = round(time.time() - start_time, 2)
    session_id = str(uuid.uuid4())[:8]

    json_path = os.path.join(OUTPUT_FOLDER, f"invoices_{session_id}.json")
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2)

    excel_path = os.path.join(OUTPUT_FOLDER, f"invoices_{session_id}.xlsx")
    export_to_excel(results, excel_path)

    return jsonify({
        "results": results,
        "processing_time": elapsed,
        "total_files": len(files),
        "session_id": session_id,
        "summary": {
            "success":  sum(1 for r in results if r["status"] == "success"),
            "warnings": sum(1 for r in results if r["status"] == "warning"),
            "errors":   sum(1 for r in results if r["status"] == "error"),
        },
    })


@app.route("/download/excel/<session_id>")
def download_excel(session_id):
    if not re.match(r'^[a-f0-9]{8}$', session_id):
        return jsonify({"error": "Invalid session"}), 400
    path = os.path.join(OUTPUT_FOLDER, f"invoices_{session_id}.xlsx")
    if not os.path.exists(path):
        return jsonify({"error": "File not found or expired"}), 404
    return send_file(path, as_attachment=True, download_name="invoices.xlsx")


@app.route("/download/json/<session_id>")
def download_json(session_id):
    if not re.match(r'^[a-f0-9]{8}$', session_id):
        return jsonify({"error": "Invalid session"}), 400
    path = os.path.join(OUTPUT_FOLDER, f"invoices_{session_id}.json")
    if not os.path.exists(path):
        return jsonify({"error": "File not found or expired"}), 404
    return send_file(path, as_attachment=True, download_name="invoices.json")


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 InvoiceIQ running on http://localhost:{port}")
    app.run(debug=False, host="0.0.0.0", port=port)
