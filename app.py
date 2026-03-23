import os
import sys
import sqlite3
from datetime import date, timedelta

from flask import Flask, g, jsonify, redirect, render_template, request, url_for

app = Flask(__name__)
DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "food.db")

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
    db.commit()
    db.close()


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
    stored_fmt = stored_date.strftime("%m/%d/%y")
    expiry_fmt = expiration_date.strftime("%m/%d/%y")
    zpl = ZPL_TEMPLATE.format(name=name, stored=stored_fmt, expiry=expiry_fmt)

    if sys.platform == "linux" and os.path.exists(PRINTER_PATH):
        try:
            with open(PRINTER_PATH, "wb") as printer:
                printer.write(zpl.encode())
        except IOError as e:
            print(f"[WARN] Printer error: {e}")
    else:
        print("[DEV] Would print ZPL label:")
        print(zpl)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM items WHERE done = 0 ORDER BY expiration_date ASC"
    ).fetchall()
    names = db.execute(
        "SELECT DISTINCT name FROM items ORDER BY name"
    ).fetchall()

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

    return render_template("index.html", items=items, names=names)


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
    name = request.form.get("name", "").strip()
    shelf_life = request.form.get("shelf_life", type=int)
    if not name or shelf_life is None or shelf_life < 0:
        return redirect(url_for("index"))

    today = date.today()
    expiration = today + timedelta(days=shelf_life)

    db = get_db()
    db.execute(
        "INSERT INTO items (name, stored_date, expiration_date) VALUES (?, ?, ?)",
        (name, today.isoformat(), expiration.isoformat()),
    )
    db.commit()
    print_label(name, today, expiration)
    return redirect(url_for("index"))


@app.route("/done/<int:item_id>", methods=["POST"])
def done(item_id):
    db = get_db()
    db.execute("UPDATE items SET done = 1 WHERE id = ?", (item_id,))
    db.commit()
    return redirect(url_for("index"))


@app.route("/print/<int:item_id>", methods=["POST"])
def reprint(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item:
        print_label(
            item["name"],
            date.fromisoformat(item["stored_date"]),
            date.fromisoformat(item["expiration_date"]),
        )
    return redirect(url_for("index"))


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
