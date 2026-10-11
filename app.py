"""MVP de facturación CFDI 4.0 con Facturapi (SANDBOX).

Supuestos (ajustables):
- Precios YA incluyen IVA (tax_included=True).
- Receptor genérico XAXX010101000 / PUBLICO EN GENERAL / régimen 616 / uso S01
  (el SAT no acepta G03 con RFC genérico).
- Con receptor genérico CFDI 4.0 exige el nodo "global" (factura global); se usa
  periodicidad diaria con el mes y año actuales.
- CP del receptor = CP de expedición de tu organización (se consulta a Facturapi).
- Cada ticket se factura una sola vez; si ya existe, se devuelve la factura existente.
"""
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import (Flask, g, has_request_context, jsonify, request, send_file,
                   send_from_directory)

load_dotenv()
API_KEY = os.getenv("FACTURAPI_KEY", "")
API = "https://www.facturapi.io/v2"
BASE = Path(__file__).parent
DB = BASE / "pos.db"
FILES = BASE / "facturas"
FILES.mkdir(exist_ok=True)

app = Flask(__name__, static_folder="static")


def headers():
    return {"Authorization": f"Bearer {API_KEY}"}


def db():
    """Conexión por petición (se cierra en teardown); fuera de petición, una nueva."""
    if not has_request_context():
        con = sqlite3.connect(DB)
        con.row_factory = sqlite3.Row
        return con
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    con = g.pop("db", None)
    if con is not None:
        con.close()


def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS productos(
        id INTEGER PRIMARY KEY, nombre TEXT, precio REAL,
        clave_sat TEXT, clave_unidad TEXT, unidad TEXT, iva REAL);
    CREATE TABLE IF NOT EXISTS tickets(
        id INTEGER PRIMARY KEY, fecha TEXT);
    CREATE TABLE IF NOT EXISTS ticket_items(
        id INTEGER PRIMARY KEY, ticket_id INTEGER, producto_id INTEGER, cantidad REAL);
    CREATE TABLE IF NOT EXISTS facturas(
        id INTEGER PRIMARY KEY, ticket_id INTEGER UNIQUE, facturapi_id TEXT,
        uuid TEXT, total REAL, xml_path TEXT, pdf_path TEXT, fecha TEXT);
    """)
    if not con.execute("SELECT 1 FROM productos").fetchone():
        con.executemany(
            "INSERT INTO productos(nombre,precio,clave_sat,clave_unidad,unidad,iva) VALUES(?,?,?,?,?,?)",
            [("Café americano", 45.00, "90101501", "E48", "Servicio", 0.16),
             ("Sándwich de jamón", 85.50, "50192100", "H87", "Pieza", 0.16),
             ("Botella de agua 600ml", 18.00, "50202310", "H87", "Pieza", 0.16)])
        con.executemany("INSERT INTO tickets(id,fecha) VALUES(?,?)",
                        [(1, datetime.now().isoformat()), (2, datetime.now().isoformat())])
        con.executemany(
            "INSERT INTO ticket_items(ticket_id,producto_id,cantidad) VALUES(?,?,?)",
            [(1, 1, 2), (1, 2, 1), (2, 3, 3), (2, 1, 1)])
    con.commit()
    con.close()


_zip_cache = {}


def zip_expedicion():
    """CP de la organización (lugar de expedición)."""
    if "zip" not in _zip_cache:
        r = requests.get(f"{API}/organizations/me", headers=headers(), timeout=30)
        r.raise_for_status()
        _zip_cache["zip"] = r.json()["legal"]["address"]["zip"]
    return _zip_cache["zip"]


def armar_payload(items):
    now = datetime.now()
    return {
        "type": "I",
        "payment_method": "PUE",
        "payment_form": "01",
        "use": "S01",
        "currency": "MXN",
        "customer": {
            "legal_name": "PUBLICO EN GENERAL",
            "tax_id": "XAXX010101000",
            "tax_system": "616",
            "address": {"zip": zip_expedicion()},
        },
        "global": {"periodicity": "day", "months": f"{now.month:02d}", "year": now.year},
        "items": [{
            "quantity": it["cantidad"],
            "product": {
                "description": it["nombre"],
                "product_key": it["clave_sat"],
                "unit_key": it["clave_unidad"],
                "unit_name": it["unidad"],
                "price": it["precio"],
                "tax_included": True,
                "taxes": [{"type": "IVA", "rate": it["iva"]}],
            },
        } for it in items],
    }


def factura_json(f):
    return {"ticket_id": f["ticket_id"], "uuid": f["uuid"], "total": f["total"],
            "xml_url": f"/api/facturas/{f['ticket_id']}/xml",
            "pdf_url": f"/api/facturas/{f['ticket_id']}/pdf"}


@app.post("/api/facturar")
def facturar():
    if not API_KEY:
        return jsonify(error="Falta FACTURAPI_KEY en .env"), 500
    try:
        ticket_id = int((request.get_json(silent=True) or {}).get("ticket_id"))
    except (TypeError, ValueError):
        return jsonify(error="ID de ticket inválido"), 400

    con = db()
    if not con.execute("SELECT 1 FROM tickets WHERE id=?", (ticket_id,)).fetchone():
        return jsonify(error="El ticket no existe"), 404
    ya = con.execute("SELECT * FROM facturas WHERE ticket_id=?", (ticket_id,)).fetchone()
    if ya:
        return jsonify(**factura_json(ya), ya_facturado=True)

    items = con.execute("""SELECT p.*, ti.cantidad FROM ticket_items ti
                           JOIN productos p ON p.id=ti.producto_id
                           WHERE ti.ticket_id=?""", (ticket_id,)).fetchall()
    if not items:
        return jsonify(error="El ticket no tiene productos"), 400

    try:
        r = requests.post(f"{API}/invoices", json=armar_payload(items),
                          headers=headers(), timeout=60)
    except requests.RequestException as e:
        return jsonify(error=f"No se pudo contactar a Facturapi: {e}"), 502
    if not r.ok:
        return jsonify(error="Facturapi rechazó la factura",
                       detalle=r.json() if r.content else r.text), 502

    inv = r.json()
    fid = inv["id"]
    paths = {}
    for kind in ("xml", "pdf"):
        d = requests.get(f"{API}/invoices/{fid}/{kind}", headers=headers(), timeout=60)
        d.raise_for_status()
        p = FILES / f"ticket_{ticket_id}.{kind}"
        p.write_bytes(d.content)
        paths[kind] = str(p)

    con.execute("""INSERT INTO facturas(ticket_id,facturapi_id,uuid,total,xml_path,pdf_path,fecha)
                   VALUES(?,?,?,?,?,?,?)""",
                (ticket_id, fid, inv.get("uuid"), inv.get("total"),
                 paths["xml"], paths["pdf"], datetime.now().isoformat()))
    con.commit()
    f = con.execute("SELECT * FROM facturas WHERE ticket_id=?", (ticket_id,)).fetchone()
    return jsonify(**factura_json(f), ya_facturado=False)


@app.get("/api/facturas")
def listar():
    rows = db().execute("SELECT * FROM facturas ORDER BY id DESC").fetchall()
    return jsonify([factura_json(f) | {"fecha": f["fecha"]} for f in rows])


@app.get("/api/facturas/<int:ticket_id>/<kind>")
def descargar(ticket_id, kind):
    if kind not in ("xml", "pdf"):
        return jsonify(error="Tipo inválido"), 400
    f = db().execute("SELECT * FROM facturas WHERE ticket_id=?", (ticket_id,)).fetchone()
    if not f:
        return jsonify(error="Factura no encontrada"), 404
    return send_file(f[f"{kind}_path"], as_attachment=False)


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
