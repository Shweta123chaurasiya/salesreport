"""Web app and daily WhatsApp digests for Uttar Pradesh mandi prices."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator

import requests
from flask import Flask, jsonify, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash


STATE = "Uttar Pradesh"
DEFAULT_DISTRICT = "Chandauli"
COMMODITIES = ["Onion", "Potato", "Tomato", "Rice", "Wheat", "Garlic"]
API_URL = "https://api.data.gov.in/resource/9ef84268-d588-465a-a308-a864a43d0070"
DATABASE_PATH = Path(
    os.environ.get("GROCERY_DB_PATH", Path(__file__).with_name("grocery_prices.sqlite3"))
)
WHATSAPP_API_URL = "https://graph.facebook.com"

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "").lower() == "true",
)
LISTING_UNITS = {"kg", "g", "piece", "dozen", "litre", "pack", "quintal"}
CATEGORIES = COMMODITIES + ["Other"]


@contextmanager
def database_connection() -> Iterator[sqlite3.Connection]:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def initialize_database() -> None:
    with database_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS prices (
                price_date TEXT NOT NULL,
                commodity TEXT NOT NULL,
                district TEXT NOT NULL,
                market TEXT NOT NULL,
                price_per_kg REAL NOT NULL,
                PRIMARY KEY (price_date, commodity, district, market)
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                phone TEXT PRIMARY KEY,
                district TEXT NOT NULL,
                commodities TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_sent_date TEXT
            );
            CREATE TABLE IF NOT EXISTS shopkeepers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                phone TEXT NOT NULL,
                district TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shopkeeper_id INTEGER NOT NULL REFERENCES shopkeepers(id),
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                price REAL NOT NULL,
                unit TEXT NOT NULL,
                stock INTEGER NOT NULL DEFAULT 0,
                district TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS listings_district_category
                ON listings(district, category);
            """
        )


def current_shopkeeper() -> sqlite3.Row | None:
    shopkeeper_id = session.get("shopkeeper_id")
    if not isinstance(shopkeeper_id, int):
        return None
    with database_connection() as connection:
        return connection.execute(
            """
            SELECT id, shop_name, email, phone, district
            FROM shopkeepers WHERE id = ?
            """,
            (shopkeeper_id,),
        ).fetchone()


def require_shopkeeper(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        if current_shopkeeper() is None:
            return jsonify({"error": "Please sign in as a shopkeeper."}), 401
        return view(*args, **kwargs)

    return wrapped


def valid_listing(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    category = payload.get("category")
    unit = payload.get("unit")
    district = payload.get("district")
    description = payload.get("description", "")
    try:
        price = float(payload.get("price"))
        stock = int(payload.get("stock"))
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name.strip()) > 100
        or not isinstance(category, str)
        or category not in CATEGORIES
        or not isinstance(unit, str)
        or unit not in LISTING_UNITS
        or not isinstance(district, str)
        or not district.strip()
        or len(district.strip()) > 80
        or not isinstance(description, str)
        or len(description) > 500
        or not math.isfinite(price)
        or price <= 0
        or price > 1_000_000
        or stock < 0
        or stock > 1_000_000
    ):
        return None
    return {
        "name": name.strip(),
        "category": category,
        "description": description.strip(),
        "price": round(price, 2),
        "unit": unit,
        "stock": stock,
        "district": district.strip(),
    }


@app.before_request
def ensure_database() -> None:
    initialize_database()


def parse_arrival_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    for date_format in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(value.strip(), date_format).date().isoformat()
        except ValueError:
            continue
    return None


def fetch_mandi_prices(api_key: str) -> list[dict[str, Any]]:
    """Fetch available UP mandi records and convert quintal rates to kg rates."""
    prices: list[dict[str, Any]] = []
    skipped_records = 0

    for commodity in COMMODITIES:
        response = requests.get(
            API_URL,
            params={
                "api-key": api_key,
                "format": "json",
                "limit": 1000,
                "sort[arrival_date]": "desc",
                "filters[state.keyword]": STATE,
                "filters[commodity]": commodity,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        records = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            raise RuntimeError(f"Unexpected data.gov.in response for {commodity}.")

        for record in records:
            if not isinstance(record, dict):
                skipped_records += 1
                continue
            try:
                price_per_kg = float(str(record["modal_price"]).replace(",", "")) / 100
                price_date = parse_arrival_date(record.get("arrival_date"))
                district = str(record["district"]).strip()
                market = str(record["market"]).strip()
            except (KeyError, TypeError, ValueError):
                skipped_records += 1
                continue

            if (
                price_per_kg <= 0
                or price_date is None
                or not district
                or not market
            ):
                skipped_records += 1
                continue

            prices.append(
                {
                    "price_date": price_date,
                    "commodity": commodity,
                    "district": district,
                    "market": market,
                    "price_per_kg": round(price_per_kg, 2),
                }
            )

    if skipped_records:
        print(f"Skipped {skipped_records} incomplete or invalid mandi records.")
    return prices


def save_prices(prices: list[dict[str, Any]]) -> None:
    with database_connection() as connection:
        connection.executemany(
            """
            INSERT OR REPLACE INTO prices
                (price_date, commodity, district, market, price_per_kg)
            VALUES (:price_date, :commodity, :district, :market, :price_per_kg)
            """,
            prices,
        )


def latest_prices(district: str | None = None) -> list[dict[str, Any]]:
    query = """
        SELECT prices.price_date, prices.commodity, prices.district,
               prices.market, prices.price_per_kg
        FROM prices
        JOIN (
            SELECT commodity, district, market, MAX(price_date) AS latest_date
            FROM prices
            GROUP BY commodity, district, market
        ) AS latest
          ON prices.commodity = latest.commodity
         AND prices.district = latest.district
         AND prices.market = latest.market
         AND prices.price_date = latest.latest_date
    """
    parameters: tuple[str, ...] = ()
    if district:
        query += " WHERE lower(prices.district) = lower(?)"
        parameters = (district,)
    query += " ORDER BY prices.commodity, prices.price_per_kg, prices.market"

    with database_connection() as connection:
        rows = connection.execute(query, parameters).fetchall()
    return [dict(row) for row in rows]


def normalize_phone(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    digits = re.sub(r"\D", "", value)
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    if len(digits) != 10 or digits[0] not in "6789":
        return None
    return f"+91{digits}"


def valid_commodity_selection(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not value:
        return None
    if any(not isinstance(item, str) or item not in COMMODITIES for item in value):
        return None
    return list(dict.fromkeys(value))


def build_digest(district: str, commodities: list[str]) -> str:
    district_prices = latest_prices(district)
    lines: list[str] = []
    for commodity in commodities:
        matching = [row for row in district_prices if row["commodity"] == commodity]
        if not matching:
            lines.append(f"{commodity}: no mandi rate available")
            continue
        for row in matching[:3]:
            lines.append(
                f"{commodity}: Rs {row['price_per_kg']:.2f}/kg, "
                f"{row['market']} ({row['price_date']})"
            )
    return "\n".join(lines)


def send_whatsapp_template(phone: str, district: str, digest: str) -> None:
    access_token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    template_name = os.environ.get("WHATSAPP_TEMPLATE_NAME")
    language = os.environ.get("WHATSAPP_TEMPLATE_LANGUAGE", "en_US")
    api_version = os.environ.get("WHATSAPP_API_VERSION")
    if not all((access_token, phone_number_id, template_name, api_version)):
        raise RuntimeError(
            "Set WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID, "
            "WHATSAPP_TEMPLATE_NAME, and WHATSAPP_API_VERSION."
        )

    response = requests.post(
        f"{WHATSAPP_API_URL}/{api_version}/{phone_number_id}/messages",
        headers={"Authorization": f"Bearer {access_token}"},
        json={
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": district},
                            {"type": "text", "text": digest},
                        ],
                    }
                ],
            },
        },
        timeout=30,
    )
    response.raise_for_status()


def send_daily_digests() -> int:
    with database_connection() as connection:
        subscriptions = connection.execute(
            "SELECT phone, district, commodities FROM subscriptions"
        ).fetchall()

    sent_count = 0
    failures: list[str] = []
    today = date.today().isoformat()
    for subscription in subscriptions:
        digest = build_digest(
            subscription["district"], json.loads(subscription["commodities"])
        )
        try:
            send_whatsapp_template(
                subscription["phone"], subscription["district"], digest
            )
        except requests.RequestException as error:
            failures.append(f"{subscription['phone']}: {error}")
            continue

        with database_connection() as connection:
            connection.execute(
                "UPDATE subscriptions SET last_sent_date = ? WHERE phone = ?",
                (today, subscription["phone"]),
            )
        sent_count += 1

    for failure in failures:
        print(f"WhatsApp send failed: {failure}", file=sys.stderr)
    if failures:
        raise RuntimeError(
            f"{len(failures)} WhatsApp digest(s) failed; {sent_count} sent."
        )
    return sent_count


def run_daily_refresh() -> None:
    api_key = os.environ.get("DATA_GOV_API_KEY")
    if not api_key:
        raise RuntimeError("Set DATA_GOV_API_KEY before refreshing mandi prices.")

    prices = fetch_mandi_prices(api_key)
    if not prices:
        raise RuntimeError("The mandi API returned no usable prices; nothing was sent.")
    save_prices(prices)
    print(f"Saved {len(prices)} mandi prices.")
    print(f"Sent {send_daily_digests()} WhatsApp digest(s).")


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/commodities")
def get_commodities():
    return jsonify(COMMODITIES)


@app.get("/api/districts")
def get_districts():
    with database_connection() as connection:
        rows = connection.execute(
            """
            SELECT district FROM prices
            UNION
            SELECT district FROM shopkeepers
            ORDER BY district
            """
        ).fetchall()
    districts = [row["district"] for row in rows]
    if DEFAULT_DISTRICT not in districts:
        districts.insert(0, DEFAULT_DISTRICT)
    return jsonify(districts)


@app.get("/api/prices")
def get_prices():
    district = request.args.get("district", DEFAULT_DISTRICT).strip()
    if not district:
        return jsonify({"error": "Choose a district."}), 400
    return jsonify(latest_prices(district))


@app.post("/api/auth/register")
def register_shopkeeper():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected a JSON registration form."}), 400
    shop_name = payload.get("shop_name")
    email = payload.get("email")
    phone = normalize_phone(payload.get("phone"))
    district = payload.get("district")
    password = payload.get("password")
    if not isinstance(shop_name, str) or not shop_name.strip() or len(shop_name) > 100:
        return jsonify({"error": "Enter a shop name (up to 100 characters)."}), 400
    if (
        not isinstance(email, str)
        or len(email) > 254
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip())
    ):
        return jsonify({"error": "Enter a valid email address."}), 400
    if phone is None:
        return jsonify({"error": "Enter a valid 10-digit Indian mobile number."}), 400
    if not isinstance(district, str) or not district.strip() or len(district) > 80:
        return jsonify({"error": "Choose a valid district."}), 400
    if not isinstance(password, str) or len(password) < 8 or len(password) > 128:
        return jsonify({"error": "Password must be 8 to 128 characters."}), 400

    try:
        with database_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO shopkeepers
                    (shop_name, email, phone, district, password_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    shop_name.strip(),
                    email.strip().lower(),
                    phone,
                    district.strip(),
                    generate_password_hash(password, method="pbkdf2:sha256"),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            shopkeeper_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        return jsonify({"error": "An account with that email already exists."}), 409

    session.clear()
    session["shopkeeper_id"] = shopkeeper_id
    return jsonify(
        {
            "message": "Shop account created.",
            "shopkeeper": {
                "shop_name": shop_name.strip(),
                "email": email.strip().lower(),
                "district": district.strip(),
            },
        }
    ), 201


@app.post("/api/auth/login")
def login_shopkeeper():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected a JSON sign-in form."}), 400
    email = payload.get("email")
    password = payload.get("password")
    if not isinstance(email, str) or not isinstance(password, str):
        return jsonify({"error": "Enter your email and password."}), 400

    with database_connection() as connection:
        shopkeeper = connection.execute(
            "SELECT id, password_hash FROM shopkeepers WHERE email = ?",
            (email.strip().lower(),),
        ).fetchone()
    if shopkeeper is None or not check_password_hash(
        shopkeeper["password_hash"], password
    ):
        return jsonify({"error": "Email or password is incorrect."}), 401

    session.clear()
    session["shopkeeper_id"] = shopkeeper["id"]
    return jsonify({"message": "Signed in successfully."})


@app.get("/api/auth/me")
def shopkeeper_session():
    shopkeeper = current_shopkeeper()
    if shopkeeper is None:
        return jsonify({"signed_in": False})
    return jsonify({"signed_in": True, "shopkeeper": dict(shopkeeper)})


@app.post("/api/auth/logout")
def logout_shopkeeper():
    session.clear()
    return jsonify({"message": "Signed out."})


@app.get("/api/listings")
def get_listings():
    district = request.args.get("district", "").strip()
    category = request.args.get("category", "").strip()
    search = request.args.get("search", "").strip()
    if category and category not in CATEGORIES:
        return jsonify({"error": "Choose a supported grocery category."}), 400

    conditions = ["listings.stock > 0"]
    parameters: list[Any] = []
    if district:
        conditions.append("lower(listings.district) = lower(?)")
        parameters.append(district)
    if category:
        conditions.append("listings.category = ?")
        parameters.append(category)
    if search:
        conditions.append(
            "(listings.name LIKE ? OR listings.description LIKE ? OR shopkeepers.shop_name LIKE ?)"
        )
        search_pattern = f"%{search[:80]}%"
        parameters.extend([search_pattern, search_pattern, search_pattern])

    with database_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT listings.id, listings.name, listings.category,
                   listings.description, listings.price, listings.unit,
                   listings.stock, listings.district, listings.updated_at,
                   shopkeepers.shop_name, shopkeepers.phone
            FROM listings
            JOIN shopkeepers ON shopkeepers.id = listings.shopkeeper_id
            WHERE {" AND ".join(conditions)}
            ORDER BY listings.updated_at DESC
            LIMIT 200
            """,
            parameters,
        ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.get("/api/dashboard")
@require_shopkeeper
def shopkeeper_dashboard():
    shopkeeper = current_shopkeeper()
    assert shopkeeper is not None
    with database_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, name, category, description, price, unit, stock,
                   district, updated_at
            FROM listings
            WHERE shopkeeper_id = ?
            ORDER BY updated_at DESC
            """,
            (shopkeeper["id"],),
        ).fetchall()
    return jsonify({"shopkeeper": dict(shopkeeper), "listings": [dict(r) for r in rows]})


@app.post("/api/listings")
@require_shopkeeper
def create_listing():
    payload = request.get_json(silent=True)
    listing = valid_listing(payload)
    if listing is None:
        return jsonify({"error": "Check the listing fields and try again."}), 400
    shopkeeper = current_shopkeeper()
    assert shopkeeper is not None
    now = datetime.now().isoformat(timespec="seconds")
    with database_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO listings
                (shopkeeper_id, name, category, description, price, unit,
                 stock, district, created_at, updated_at)
            VALUES (:shopkeeper_id, :name, :category, :description, :price, :unit,
                    :stock, :district, :created_at, :updated_at)
            """,
            {
                **listing,
                "shopkeeper_id": shopkeeper["id"],
                "created_at": now,
                "updated_at": now,
            },
        )
        listing_id = cursor.lastrowid
    return jsonify({"id": listing_id, "message": "Listing published."}), 201


@app.put("/api/listings/<int:listing_id>")
@require_shopkeeper
def update_listing(listing_id: int):
    listing = valid_listing(request.get_json(silent=True))
    if listing is None:
        return jsonify({"error": "Check the listing fields and try again."}), 400
    shopkeeper = current_shopkeeper()
    assert shopkeeper is not None
    with database_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE listings SET name = :name, category = :category,
                description = :description, price = :price, unit = :unit,
                stock = :stock, district = :district, updated_at = :updated_at
            WHERE id = :id AND shopkeeper_id = :shopkeeper_id
            """,
            {
                **listing,
                "id": listing_id,
                "shopkeeper_id": shopkeeper["id"],
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "Listing not found in your shop."}), 404
    return jsonify({"message": "Listing updated."})


@app.delete("/api/listings/<int:listing_id>")
@require_shopkeeper
def delete_listing(listing_id: int):
    shopkeeper = current_shopkeeper()
    assert shopkeeper is not None
    with database_connection() as connection:
        cursor = connection.execute(
            "DELETE FROM listings WHERE id = ? AND shopkeeper_id = ?",
            (listing_id, shopkeeper["id"]),
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "Listing not found in your shop."}), 404
    return jsonify({"message": "Listing removed."})


@app.post("/api/subscribe")
def subscribe():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected a JSON subscription."}), 400

    phone = normalize_phone(payload.get("phone"))
    commodities = valid_commodity_selection(payload.get("commodities"))
    district = payload.get("district")
    if phone is None:
        return jsonify({"error": "Enter a valid 10-digit Indian mobile number."}), 400
    if commodities is None:
        return jsonify({"error": "Select at least one supported grocery item."}), 400
    if not isinstance(district, str) or not district.strip() or len(district) > 80:
        return jsonify({"error": "Choose a valid Uttar Pradesh district."}), 400
    if payload.get("consent") is not True:
        return jsonify({"error": "WhatsApp opt-in consent is required."}), 400

    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO subscriptions
                (phone, district, commodities, token_hash, created_at, last_sent_date)
            VALUES (?, ?, ?, ?, ?, NULL)
            ON CONFLICT(phone) DO UPDATE SET
                district = excluded.district,
                commodities = excluded.commodities,
                token_hash = excluded.token_hash,
                created_at = excluded.created_at,
                last_sent_date = NULL
            """,
            (
                phone,
                district.strip(),
                json.dumps(commodities),
                token_hash,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
    return jsonify(
        {
            "message": "Subscription saved. Daily WhatsApp digests will start after setup.",
            "phone": phone,
            "manage_token": token,
        }
    ), 201


@app.post("/api/unsubscribe")
def unsubscribe():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected JSON subscription details."}), 400
    phone = normalize_phone(payload.get("phone"))
    token = payload.get("manage_token")
    if phone is None or not isinstance(token, str):
        return jsonify({"error": "Invalid phone number or subscription token."}), 400

    with database_connection() as connection:
        subscription = connection.execute(
            "SELECT token_hash FROM subscriptions WHERE phone = ?", (phone,)
        ).fetchone()
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if subscription is None or not secrets.compare_digest(
            subscription["token_hash"], token_hash
        ):
            return jsonify({"error": "Subscription not found or token is invalid."}), 404
        connection.execute("DELETE FROM subscriptions WHERE phone = ?", (phone,))
    return jsonify({"message": "Your daily WhatsApp digest is cancelled."})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        choices=("serve", "refresh"),
        default="serve",
        help="'serve' runs the web app; 'refresh' updates prices and sends digests.",
    )
    args = parser.parse_args()

    if args.command == "refresh":
        run_daily_refresh()
        return

    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )


if __name__ == "__main__":
    main()
