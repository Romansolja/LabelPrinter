import os
import sys
import sqlite3
from datetime import date, timedelta

from flask import Flask, g, jsonify, redirect, render_template, request, url_for

app = Flask(__name__)
DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "food.db")


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
    # Seed catalog from existing items history (one-time).
    # We must normalize names in Python (title-case) because SQL can't do that,
    # then merge duplicates that collapse to the same normalized form.
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
            INSERT INTO catalog (name, shelf_life_days, use_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                use_count = use_count + excluded.use_count,
                updated_at = MAX(updated_at, excluded.updated_at)
            """,
            (normalized, row[1], row[2], row[3], row[4]),
        )
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

    # Distinct category names for the modal dropdown
    category_names = sorted(categories.keys())

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


@app.route("/api/catalog/<int:catalog_id>/deactivate", methods=["POST"])
def deactivate_catalog(catalog_id):
    db = get_db()
    row = db.execute("SELECT name FROM catalog WHERE id = ?", (catalog_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Item not found"}), 404
    db.execute("UPDATE catalog SET is_active = 0 WHERE id = ?", (catalog_id,))
    db.commit()
    return jsonify({"ok": True, "name": row["name"]})


@app.route("/api/catalog/<int:catalog_id>/activate", methods=["POST"])
def activate_catalog(catalog_id):
    db = get_db()
    row = db.execute("SELECT name FROM catalog WHERE id = ?", (catalog_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Item not found"}), 404
    db.execute("UPDATE catalog SET is_active = 1 WHERE id = ?", (catalog_id,))
    db.commit()
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
    db.execute(
        "UPDATE catalog SET name = ?, category = ?, shelf_life_days = ? WHERE id = ?",
        (name, category, shelf_life_val, catalog_id),
    )
    db.commit()
    return jsonify({"ok": True, "id": catalog_id, "name": name, "category": category, "shelf_life_days": shelf_life_val})


@app.route("/api/category", methods=["POST"])
def add_category():
    """Create a new category by name. Just validates and returns — categories
    exist implicitly via catalog items, but this endpoint lets the UI confirm
    the name is valid before assigning items to it."""
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "Invalid request"}), 400
    name = data.get("name", "").strip().title()
    if not name:
        return jsonify({"ok": False, "error": "Category name cannot be empty"}), 400
    if name == "Other":
        return jsonify({"ok": False, "error": "'Other' already exists"}), 400
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
    count = db.execute("SELECT COUNT(*) FROM catalog WHERE category = ?", (old_name,)).fetchone()[0]
    if count == 0:
        return jsonify({"ok": False, "error": "Category not found"}), 404
    db.execute("UPDATE catalog SET category = ? WHERE category = ?", (new_name, old_name))
    db.commit()
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
    count = db.execute("SELECT COUNT(*) FROM catalog WHERE category = ?", (name,)).fetchone()[0]
    if count == 0:
        return jsonify({"ok": False, "error": "Category not found"}), 404
    db.execute("UPDATE catalog SET category = ? WHERE category = ?", (move_to, name))
    db.commit()
    return jsonify({"ok": True, "deleted": name, "moved_to": move_to, "items_moved": count})


@app.route("/api/catalog/all")
def get_all_catalog():
    """Return full catalog including inactive items, for the Manage Catalog view."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM catalog ORDER BY category, name"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


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


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
