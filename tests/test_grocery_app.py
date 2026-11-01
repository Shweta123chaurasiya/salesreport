import json
import tempfile
import unittest
from pathlib import Path

import grocery_app


class GroceryAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        grocery_app.DATABASE_PATH = Path(self.temp_dir.name) / "test.sqlite3"
        grocery_app.app.config.update(TESTING=True)
        grocery_app.initialize_database()
        self.client = grocery_app.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def register_shop(self, email="shop@example.com", shop_name="Chakia Store"):
        return self.client.post(
            "/api/auth/register",
            json={
                "shop_name": shop_name,
                "email": email,
                "phone": "9876543210",
                "district": "Chandauli",
                "password": "strong-pass-123",
            },
        )

    def test_normalize_indian_phone_number(self):
        self.assertEqual(grocery_app.normalize_phone("+91 98765-43210"), "+919876543210")
        self.assertIsNone(grocery_app.normalize_phone("12345"))

    def test_shopkeeper_can_register_login_and_get_dashboard(self):
        response = self.register_shop()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.client.get("/api/auth/me").get_json()["signed_in"], True)

        self.client.post("/api/auth/logout")
        login_response = self.client.post(
            "/api/auth/login",
            json={"email": "SHOP@example.com", "password": "strong-pass-123"},
        )

        self.assertEqual(login_response.status_code, 200)
        dashboard_response = self.client.get("/api/dashboard")
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertEqual(dashboard_response.get_json()["shopkeeper"]["shop_name"], "Chakia Store")

    def test_listing_creation_and_public_browse(self):
        self.register_shop()
        payload = {
            "name": "Basmati rice",
            "category": "Rice",
            "description": "Premium quality",
            "price": 80,
            "unit": "kg",
            "stock": 20,
            "district": "Chandauli",
        }

        response = self.client.post("/api/listings", json=payload)

        self.assertEqual(response.status_code, 201)
        listing_response = self.client.get(
            "/api/listings?district=Chandauli&category=Rice&search=Basmati"
        )
        listings = listing_response.get_json()
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0]["name"], "Basmati rice")
        self.assertEqual(listings[0]["shop_name"], "Chakia Store")
        self.assertEqual(listings[0]["phone"], "+919876543210")

    def test_shopkeepers_cannot_edit_or_delete_other_shop_listings(self):
        self.register_shop()
        response = self.client.post(
            "/api/listings",
            json={
                "name": "Potato",
                "category": "Potato",
                "description": "",
                "price": 25,
                "unit": "kg",
                "stock": 5,
                "district": "Chandauli",
            },
        )
        listing_id = response.get_json()["id"]
        self.client.post("/api/auth/logout")
        self.register_shop(email="another@example.com", shop_name="Another Shop")

        update_response = self.client.put(
            f"/api/listings/{listing_id}",
            json={
                "name": "Potato",
                "category": "Potato",
                "description": "",
                "price": 23,
                "unit": "kg",
                "stock": 5,
                "district": "Chandauli",
            },
        )
        delete_response = self.client.delete(f"/api/listings/{listing_id}")

        self.assertEqual(update_response.status_code, 404)
        self.assertEqual(delete_response.status_code, 404)

    def test_listing_requires_shopkeeper_login_and_valid_price(self):
        response = self.client.post(
            "/api/listings",
            json={
                "name": "Rice",
                "category": "Rice",
                "price": -1,
                "unit": "kg",
                "stock": 1,
                "district": "Chandauli",
            },
        )
        self.assertEqual(response.status_code, 401)

        self.register_shop()
        invalid_response = self.client.post(
            "/api/listings",
            json={
                "name": "Rice",
                "category": "Rice",
                "price": "NaN",
                "unit": "kg",
                "stock": 1,
                "district": "Chandauli",
            },
        )
        self.assertEqual(invalid_response.status_code, 400)

    def test_price_endpoint_returns_latest_rate_per_market(self):
        grocery_app.save_prices(
            [
                {
                    "price_date": "2026-10-18",
                    "commodity": "Potato",
                    "district": "Chandauli",
                    "market": "Chandauli",
                    "price_per_kg": 20.0,
                },
                {
                    "price_date": "2026-10-19",
                    "commodity": "Potato",
                    "district": "Chandauli",
                    "market": "Chandauli",
                    "price_per_kg": 21.5,
                },
            ]
        )

        response = self.client.get("/api/prices?district=Chandauli")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()[0]["price_per_kg"], 21.5)

    def test_subscription_requires_consent_and_saves_opted_in_user(self):
        payload = {
            "phone": "9876543210",
            "district": "Chandauli",
            "commodities": ["Potato", "Rice"],
            "consent": True,
        }
        response = self.client.post("/api/subscribe", json=payload)

        self.assertEqual(response.status_code, 201)
        with grocery_app.database_connection() as connection:
            row = connection.execute(
                "SELECT phone, commodities, token_hash FROM subscriptions"
            ).fetchone()
        self.assertEqual(row["phone"], "+919876543210")
        self.assertEqual(json.loads(row["commodities"]), ["Potato", "Rice"])
        self.assertNotEqual(row["token_hash"], response.get_json()["manage_token"])

    def test_unsubscribe_requires_subscription_token(self):
        subscribe_response = self.client.post(
            "/api/subscribe",
            json={
                "phone": "9876543210",
                "district": "Chandauli",
                "commodities": ["Potato"],
                "consent": True,
            },
        )
        credentials = subscribe_response.get_json()

        response = self.client.post(
            "/api/unsubscribe",
            json={
                "phone": credentials["phone"],
                "manage_token": credentials["manage_token"],
            },
        )

        self.assertEqual(response.status_code, 200)
        with grocery_app.database_connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM subscriptions"
            ).fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
