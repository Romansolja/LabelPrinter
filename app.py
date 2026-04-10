import os
import sys
import time
import sqlite3
from datetime import date, timedelta

import psutil
from flask import Flask, g, jsonify, redirect, render_template, request, url_for

app = Flask(__name__)
DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "food.db")

CATALOG_VERSION = 0


def bump_catalog_version():
    global CATALOG_VERSION
    CATALOG_VERSION += 1


def _normalize_name(name):
    """Strip whitespace and title-case so 'chicken', 'CHICKEN', '  Chicken  ' all become 'Chicken'."""
    return name.strip().title()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DATABASE)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            stored_date TEXT NOT NULL,
            expiration_date TEXT NOT NULL,
            done INTEGER DEFAULT 0
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL DEFAULT 'Other',
            shelf_life_days INTEGER NOT NULL DEFAULT 7,
            use_count INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (date('now')),
            updated_at TEXT NOT NULL DEFAULT (date('now'))
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (date('now'))
        )
        """
    )
    # Ensure "Other" always exists
    db.execute("INSERT OR IGNORE INTO categories (name) VALUES ('Other')")

    # Seed catalog from existing items history (one-time, idempotent).
    # INSERT OR IGNORE means rows already in catalog are untouched on restart.
    # We normalize names in Python (title-case) because SQL can't do that.
    seed_rows = db.execute(
        """
        SELECT
            TRIM(name) AS raw_name,
            CAST(julianday(expiration_date) - julianday(stored_date) AS INTEGER) AS shelf,
            COUNT(*) AS cnt,
            MIN(stored_date) AS first_date,
            MAX(stored_date) AS last_date
        FROM items
        GROUP BY TRIM(name)
        """
    ).fetchall()
    for row in seed_rows:
        normalized = row[0].strip().title()
        db.execute(
            """
            INSERT OR IGNORE INTO catalog (name, shelf_life_days, use_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (normalized, row[1], row[2], row[3], row[4]),
        )
    # Seed categories from existing catalog rows
    existing_cats = db.execute("SELECT DISTINCT category FROM catalog").fetchall()
    for cat_row in existing_cats:
        db.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (cat_row[0],))
    db.commit()
    db.close()


def _upsert_catalog(db, name, shelf_life_days):
    """Upsert a catalog entry. Only updates updated_at on actual usage (not metadata edits)."""
    db.execute(
        """
        INSERT INTO catalog (name, shelf_life_days, use_count, updated_at)
        VALUES (?, ?, 1, date('now'))
        ON CONFLICT(name) DO UPDATE SET
            shelf_life_days = excluded.shelf_life_days,
            use_count = use_count + 1,
            updated_at = date('now')
        """,
        (name, shelf_life_days),
    )


# ---------------------------------------------------------------------------
# ZPL printing
# ---------------------------------------------------------------------------

PRINTER_PATH = "/dev/usb/lp0"

ZPL_TEMPLATE = (
    "^XA\n"
    "^CF0,60\n"
    "^FO50,30^FD{name}^FS\n"
    "^CF0,40\n"
    "^FO50,120^FDStored: {stored}^FS\n"
    "^FO50,170^FDUse by: {expiry}^FS\n"
    "^XZ\n"
)


def print_label(name, stored_date, expiration_date):
    """Print a ZPL label. Returns True on success, False on failure."""
    stored_fmt = stored_date.strftime("%m/%d/%y")
    expiry_fmt = expiration_date.strftime("%m/%d/%y")
    zpl = ZPL_TEMPLATE.format(name=name, stored=stored_fmt, expiry=expiry_fmt)

    if sys.platform == "linux" and os.path.exists(PRINTER_PATH):
        try:
            with open(PRINTER_PATH, "wb") as printer:
                printer.write(zpl.encode())
            return True
        except IOError as e:
            print(f"[WARN] Printer error: {e}")
            return False
    else:
        print("[DEV] Would print ZPL label:")
        print(zpl)
        return True


def print_blank_label():
    """Print a blank ZPL label with just today's date. Returns True on success."""
    today_str = date.today().strftime("%b %d, %Y")
    zpl = (
        "^XA\n"
        "^CFA,30\n"
        f"^FO50,200^FDDate: {today_str}^FS\n"
        "^PQ1\n"
        "^XZ\n"
    )
    if sys.platform == "linux" and os.path.exists(PRINTER_PATH):
        try:
            with open(PRINTER_PATH, "wb") as printer:
                printer.write(zpl.encode())
            return True
        except IOError as e:
            print(f"[WARN] Printer error: {e}")
            return False
    else:
        print("[DEV] Would print ZPL label:")
        print(zpl)
        return True


def _is_ajax():
    return request.headers.get("X-Requested-With") == "fetch"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM items WHERE done = 0 ORDER BY expiration_date ASC"
    ).fetchall()

    # Catalog for POS grid
    catalog_rows = db.execute(
        "SELECT * FROM catalog WHERE is_active = 1 ORDER BY use_count DESC, updated_at DESC, name"
    ).fetchall()

    # Build categories dict and flat list
    categories = {}
    catalog_list = []
    for row in catalog_rows:
        entry = dict(row)
        catalog_list.append(entry)
        cat = entry["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(entry)

    # Distinct category names: merge categories table + catalog rows
    saved_cats = db.execute("SELECT name FROM categories WHERE is_active = 1").fetchall()
    all_cat_names = set(categories.keys())
    for cat_row in saved_cats:
        all_cat_names.add(cat_row["name"])
    category_names = sorted(all_cat_names)

    today = date.today()
    items = []
    for row in rows:
        exp = date.fromisoformat(row["expiration_date"])
        items.append({
            "id": row["id"],
            "name": row["name"],
            "stored_date": date.fromisoformat(row["stored_date"]).strftime("%m/%d/%y"),
            "expiration_date": exp.strftime("%m/%d/%y"),
            "days_left": (exp - today).days,
        })

    return render_template(
        "index.html",
        items=items,
        catalog=catalog_list,
        categories=categories,
        category_names=category_names,
    )


@app.route("/shelf-life/<name>")
def shelf_life(name):
    db = get_db()
    row = db.execute(
        "SELECT stored_date, expiration_date FROM items WHERE name = ? ORDER BY id DESC LIMIT 1",
        (name,),
    ).fetchone()
    if row:
        days = (date.fromisoformat(row["expiration_date"]) - date.fromisoformat(row["stored_date"])).days
        return jsonify({"days": days})
    return jsonify({"days": None})


@app.route("/add", methods=["POST"])
def add():
    name = _normalize_name(request.form.get("name", ""))
    shelf_life_val = request.form.get("shelf_life", type=int)
    if not name or shelf_life_val is None or shelf_life_val < 0:
        return redirect(url_for("index"))

    today = date.today()
    expiration = today + timedelta(days=shelf_life_val)

    db = get_db()
    db.execute(
        "INSERT INTO items (name, stored_date, expiration_date) VALUES (?, ?, ?)",
        (name, today.isoformat(), expiration.isoformat()),
    )
    _upsert_catalog(db, name, shelf_life_val)
    db.commit()
    bump_catalog_version()
    print_label(name, today, expiration)
    return redirect(url_for("index"))


@app.route("/api/quick-add", methods=["POST"])
def quick_add():
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "Invalid request"}), 400
    name = _normalize_name(data.get("name", ""))
    if not name:
        return jsonify({"ok": False, "error": "No name provided"}), 400

    db = get_db()
    cat_row = db.execute(
        "SELECT shelf_life_days FROM catalog WHERE name = ?", (name,)
    ).fetchone()
    if not cat_row:
        return jsonify({"ok": False, "error": "Unknown item"}), 400

    shelf_life_val = cat_row["shelf_life_days"]
    today = date.today()
    expiration = today + timedelta(days=shelf_life_val)

    db.execute(
        "INSERT INTO items (name, stored_date, expiration_date) VALUES (?, ?, ?)",
        (name, today.isoformat(), expiration.isoformat()),
    )
    _upsert_catalog(db, name, shelf_life_val)
    db.commit()
    bump_catalog_version()

    printed = print_label(name, today, expiration)
    item_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    return jsonify({
        "ok": True,
        "printed": printed,
        "item": {
            "id": item_id,
            "name": name,
            "stored_date": today.strftime("%m/%d/%y"),
            "expiration_date": expiration.strftime("%m/%d/%y"),
            "days_left": shelf_life_val,
        },
    })


@app.route("/api/catalog", methods=["POST"])
def add_catalog():
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "Invalid request"}), 400
    name = _normalize_name(data.get("name", ""))
    category = data.get("category", "Other").strip().title()
    if not name:
        return jsonify({"ok": False, "error": "No name provided"}), 400
    try:
        shelf_life_val = int(data.get("shelf_life_days", 7))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Shelf life must be a number"}), 400
    if shelf_life_val < 0 or shelf_life_val > 365:
        return jsonify({"ok": False, "error": "Shelf life must be 0-365 days"}), 400
    if not category:
        category = "Other"

    db = get_db()
    db.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (category,))
    db.execute(
        """
        INSERT INTO catalog (name, category, shelf_life_days)
        VALUES (?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            category = excluded.category,
            shelf_life_days = excluded.shelf_life_days
        """,
        (name, category, shelf_life_val),
    )
    db.commit()
    bump_catalog_version()
    return jsonify({"ok": True, "name": name, "category": category, "shelf_life_days": shelf_life_val})


@app.route("/api/print-once", methods=["POST"])
def print_once():
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "Invalid request"}), 400
    name = _normalize_name(data.get("name", ""))
    if not name:
        return jsonify({"ok": False, "error": "No name provided"}), 400
    try:
        shelf_life_val = int(data.get("shelf_life_days", 7))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Shelf life must be a number"}), 400
    if shelf_life_val < 0 or shelf_life_val > 365:
        return jsonify({"ok": False, "error": "Shelf life must be 0-365 days"}), 400

    today = date.today()
    expiration = today + timedelta(days=shelf_life_val)
    printed = print_label(name, today, expiration)
    return jsonify({"ok": True, "printed": printed})


@app.route("/api/print-blank", methods=["POST"])
def print_blank():
    printed = print_blank_label()
    return jsonify({"ok": True, "printed": printed})


@app.route("/api/catalog/<int:catalog_id>/deactivate", methods=["POST"])
def deactivate_catalog(catalog_id):
    db = get_db()
    row = db.execute("SELECT name FROM catalog WHERE id = ?", (catalog_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Item not found"}), 404
    db.execute("UPDATE catalog SET is_active = 0 WHERE id = ?", (catalog_id,))
    db.commit()
    bump_catalog_version()
    return jsonify({"ok": True, "name": row["name"]})


@app.route("/api/catalog/<int:catalog_id>/activate", methods=["POST"])
def activate_catalog(catalog_id):
    db = get_db()
    row = db.execute("SELECT name FROM catalog WHERE id = ?", (catalog_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Item not found"}), 404
    db.execute("UPDATE catalog SET is_active = 1 WHERE id = ?", (catalog_id,))
    db.commit()
    bump_catalog_version()
    return jsonify({"ok": True, "name": row["name"]})


@app.route("/api/catalog/<int:catalog_id>/delete", methods=["POST"])
def delete_catalog(catalog_id):
    """Permanently delete a catalog item. Does NOT delete printed item history."""
    db = get_db()
    row = db.execute("SELECT name FROM catalog WHERE id = ?", (catalog_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Item not found"}), 404
    db.execute("DELETE FROM catalog WHERE id = ?", (catalog_id,))
    db.commit()
    bump_catalog_version()
    return jsonify({"ok": True, "name": row["name"]})


@app.route("/api/catalog/<int:catalog_id>", methods=["POST"])
def update_catalog_item(catalog_id):
    """Update an existing catalog item's name, category, or shelf life."""
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "Invalid request"}), 400
    db = get_db()
    row = db.execute("SELECT * FROM catalog WHERE id = ?", (catalog_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Item not found"}), 404

    name = _normalize_name(data.get("name", row["name"]))
    category = data.get("category", row["category"]).strip().title()
    if not name:
        return jsonify({"ok": False, "error": "Name cannot be empty"}), 400
    try:
        shelf_life_val = int(data.get("shelf_life_days", row["shelf_life_days"]))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Shelf life must be a number"}), 400
    if shelf_life_val < 0 or shelf_life_val > 365:
        return jsonify({"ok": False, "error": "Shelf life must be 0-365 days"}), 400
    if not category:
        category = "Other"

    # Don't update updated_at here — that's only for actual usage
    db.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (category,))
    db.execute(
        "UPDATE catalog SET name = ?, category = ?, shelf_life_days = ? WHERE id = ?",
        (name, category, shelf_life_val, catalog_id),
    )
    db.commit()
    bump_catalog_version()
    return jsonify({"ok": True, "id": catalog_id, "name": name, "category": category, "shelf_life_days": shelf_life_val})


@app.route("/api/category", methods=["POST"])
def add_category():
    """Create a new category and persist it to the categories table."""
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "Invalid request"}), 400
    name = data.get("name", "").strip().title()
    if not name:
        return jsonify({"ok": False, "error": "Category name cannot be empty"}), 400
    if name == "Other":
        return jsonify({"ok": False, "error": "'Other' already exists"}), 400
    db = get_db()
    existing = db.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
    if existing:
        return jsonify({"ok": False, "error": "Category already exists"}), 400
    db.execute("INSERT INTO categories (name) VALUES (?)", (name,))
    db.commit()
    bump_catalog_version()
    return jsonify({"ok": True, "category": name})


@app.route("/api/category/rename", methods=["POST"])
def rename_category():
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "Invalid request"}), 400
    old_name = data.get("old_name", "").strip().title()
    new_name = data.get("new_name", "").strip().title()
    if not old_name or not new_name:
        return jsonify({"ok": False, "error": "Both old and new names required"}), 400
    if old_name == "Other":
        return jsonify({"ok": False, "error": "Cannot rename 'Other'"}), 400

    db = get_db()
    cat_row = db.execute("SELECT id FROM categories WHERE name = ?", (old_name,)).fetchone()
    count = db.execute("SELECT COUNT(*) FROM catalog WHERE category = ?", (old_name,)).fetchone()[0]
    if not cat_row and count == 0:
        return jsonify({"ok": False, "error": "Category not found"}), 404
    db.execute("UPDATE catalog SET category = ? WHERE category = ?", (new_name, old_name))
    db.execute("UPDATE categories SET name = ? WHERE name = ?", (new_name, old_name))
    # Ensure the new name exists in categories even if only catalog rows had it
    db.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (new_name,))
    db.commit()
    bump_catalog_version()
    return jsonify({"ok": True, "old_name": old_name, "new_name": new_name, "items_moved": count})


@app.route("/api/category/delete", methods=["POST"])
def delete_category():
    """Delete a category by moving all its items to a destination category (default: Other)."""
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "Invalid request"}), 400
    name = data.get("name", "").strip().title()
    move_to = data.get("move_to", "Other").strip().title()
    if not name:
        return jsonify({"ok": False, "error": "Category name required"}), 400
    if name == "Other":
        return jsonify({"ok": False, "error": "Cannot delete 'Other'"}), 400
    if not move_to:
        move_to = "Other"

    db = get_db()
    cat_row = db.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
    count = db.execute("SELECT COUNT(*) FROM catalog WHERE category = ?", (name,)).fetchone()[0]
    if not cat_row and count == 0:
        return jsonify({"ok": False, "error": "Category not found"}), 404
    db.execute("UPDATE catalog SET category = ? WHERE category = ?", (move_to, name))
    db.execute("DELETE FROM categories WHERE name = ?", (name,))
    db.commit()
    bump_catalog_version()
    return jsonify({"ok": True, "deleted": name, "moved_to": move_to, "items_moved": count})


@app.route("/api/sync-state")
def sync_state():
    return jsonify({"catalog_version": CATALOG_VERSION})


def _safe(fn, *args, **kwargs):
    """Run a metric collector; return None on any failure. Never raises."""
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def _read_cpu_temp_c():
    # Pi exposes millidegrees C in this file. Not present on Windows dev machine.
    with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
        return round(int(f.read().strip()) / 1000.0, 1)


@app.route("/api/system-stats")
def api_system_stats():
    cpu_pct = _safe(psutil.cpu_percent, interval=None)  # non-blocking
    vm = _safe(psutil.virtual_memory)
    disk = _safe(psutil.disk_usage, "/")
    boot_time = _safe(psutil.boot_time)
    temp_c = _safe(_read_cpu_temp_c)

    uptime_seconds = None
    if boot_time is not None:
        try:
            uptime_seconds = int(time.time() - boot_time)
        except Exception:
            uptime_seconds = None

    return jsonify({
        "cpu_percent":    cpu_pct,
        "cpu_temp_c":     temp_c,
        "ram_percent":    vm.percent if vm else None,
        "ram_used_mb":    int(vm.used / (1024 * 1024)) if vm else None,
        "ram_total_mb":   int(vm.total / (1024 * 1024)) if vm else None,
        "disk_percent":   disk.percent if disk else None,
        "disk_used_gb":   round(disk.used / (1024 ** 3), 1) if disk else None,
        "disk_total_gb":  round(disk.total / (1024 ** 3), 1) if disk else None,
        "uptime_seconds": uptime_seconds,
        "server_time":    int(time.time()),  # epoch seconds, set after collection
    })


@app.route("/api/catalog/all")
def get_all_catalog():
    """Return full catalog including inactive items, for the Manage Catalog view."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM catalog ORDER BY category, name"
    ).fetchall()
    catalog_items = [dict(r) for r in rows]
    # Include all saved categories so empty ones appear in manage view
    cat_rows = db.execute("SELECT name FROM categories WHERE is_active = 1 ORDER BY name").fetchall()
    all_categories = [r["name"] for r in cat_rows]
    return jsonify({"items": catalog_items, "categories": all_categories})


@app.route("/done/<int:item_id>", methods=["POST"])
def done(item_id):
    db = get_db()
    db.execute("UPDATE items SET done = 1 WHERE id = ?", (item_id,))
    db.commit()
    if _is_ajax():
        return jsonify({"ok": True})
    return redirect(url_for("index"))


@app.route("/print/<int:item_id>", methods=["POST"])
def reprint(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    printed = False
    if item:
        printed = print_label(
            item["name"],
            date.fromisoformat(item["stored_date"]),
            date.fromisoformat(item["expiration_date"]),
        )
    if _is_ajax():
        return jsonify({"ok": True, "printed": printed})
    return redirect(url_for("index"))


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
