"""Seed the Bharat Bazaar demo shop with deterministic, canary-tagged data.

Every customer is a potential erasure target. Each carries a unique canary token
(the first 12 hex characters of a seeded UUID) inside their personal-data fields. After
erasing one customer, a scan for their token must find nothing (the canary check), and
every other customer's rows must be unchanged (the bystander check). For that to mean
anything, no row may ever carry two customers' tokens; the unit tests enforce this.

The same seed always produces the same data, so a failing test reproduces exactly.

Usage:
    uv run python examples/bharat-bazaar/seed.py
    uv run python examples/bharat-bazaar/seed.py --customers 500 --seed 7

The loader drops and recreates the `public` schema, so it refuses to run against any
database not named `bharat_bazaar` unless you pass --allow-any-database.
"""

from __future__ import annotations

import argparse
import os
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from faker import Faker
from psycopg import sql
from psycopg.types.json import Jsonb

DEFAULT_DSN = "postgresql://vilopa:vilopa@127.0.0.1:55432/bharat_bazaar"
DEMO_DATABASE = "bharat_bazaar"
SCHEMA_SQL = Path(__file__).with_name("schema.sql")
EPOCH = datetime(2026, 9, 1, tzinfo=UTC)  # fixed "now" for generated data

Row = dict[str, Any]
Dataset = dict[str, list[Row]]

# Parent tables first: this is also the load order.
TABLES = (
    "customers",
    "addresses",
    "products",
    "orders",
    "order_items",
    "invoices",
    "payments",
    "reviews",
    "support_tickets",
    "wishlists",
    "events",
)
TABLES_WITH_IDENTITY = tuple(t for t in TABLES if t != "wishlists")

PRODUCTS = (
    "Cotton kurta",
    "Brass diya set",
    "Masala dabba",
    "Handloom saree",
    "Copper water bottle",
    "Block-print bedsheet",
    "Terracotta planter",
    "Jute tote bag",
    "Sandalwood soap",
    "Filter coffee set",
    "Banarasi dupatta",
    "Madhubani wall art",
)


@dataclass(frozen=True)
class Canary:
    """Unique, searchable markers for one seeded person."""

    id: uuid.UUID

    @property
    def token(self) -> str:
        return self.id.hex[:12]

    @property
    def email(self) -> str:
        # Unhyphenated hex, so the token is a plain substring a canary scan can find.
        return f"c-{self.id.hex}@example.test"

    @property
    def mobile(self) -> str:
        """Ten-digit Indian mobile number derived from the UUID."""
        n = self.id.int
        return "6789"[n % 4] + f"{(n // 4) % 10**9:09d}"


def phone_variants(mobile: str) -> tuple[str, ...]:
    """The formats real Indian data arrives in; normalisers must treat them as equal."""
    return (f"+91{mobile}", f"+91 {mobile[:5]} {mobile[5:]}", f"0{mobile}", mobile)


def generate(seed: int = 42, customers: int = 200) -> Dataset:
    """Build the whole dataset in memory. Pure: no I/O, same input gives same output."""
    rng = random.Random(seed)
    fake = Faker("en_IN")
    fake.seed_instance(seed)
    data: Dataset = {table: [] for table in TABLES}
    ids = {table: 0 for table in TABLES_WITH_IDENTITY}

    def next_id(table: str) -> int:
        ids[table] += 1
        return ids[table]

    def ts(max_days_ago: int, min_days_ago: int = 0) -> datetime:
        seconds = rng.randint(min_days_ago * 86400, max_days_ago * 86400)
        return EPOCH - timedelta(seconds=seconds)

    for name in PRODUCTS:
        pid = next_id("products")
        data["products"].append(
            {
                "id": pid,
                "sku": f"BB-{pid:04d}",
                "name": name,
                "price_paise": rng.randint(199, 9999) * 100,
            }
        )
    product_ids = [p["id"] for p in data["products"]]
    prices = {p["id"]: p["price_paise"] for p in data["products"]}

    canaries = _unique_canaries(rng, customers)

    for canary in canaries:
        cid = next_id("customers")
        first, last = fake.first_name(), fake.last_name()
        created = ts(1500, 30)
        inactive = rng.random() < 0.15  # candidates for retention sweeps
        data["customers"].append(
            {
                "id": cid,
                "email": canary.email,
                "phone": f"+91{canary.mobile}",
                "full_name": f"{first} {last} {canary.token}",
                "date_of_birth": fake.date_of_birth(minimum_age=18, maximum_age=75),
                "pan": _pan(rng, last) if rng.random() < 0.5 else None,
                "referred_by_customer_id": (
                    rng.randint(1, cid - 1) if cid > 1 and rng.random() < 0.2 else None
                ),
                "created_at": created,
                "last_login_at": ts(1500, 1100) if inactive else ts(60),
            }
        )

        address_ids = []
        for n in range(rng.randint(1, 3)):
            aid = next_id("addresses")
            address_ids.append(aid)
            data["addresses"].append(
                {
                    "id": aid,
                    "customer_id": cid,
                    "line1": f"{fake.building_number()}, {fake.street_name()}",
                    "line2": f"Near {fake.street_name()} temple, ref {canary.token}",
                    "city": fake.city(),
                    "state": fake.state(),
                    "pin_code": fake.postcode(),
                    "phone": rng.choice(phone_variants(canary.mobile)),
                    "is_default": n == 0,
                }
            )

        for _ in range(rng.randint(0, 6)):
            oid = next_id("orders")
            address_id = rng.choice(address_ids)
            placed = ts(700)
            status = rng.choice(("placed", "shipped", "delivered", "delivered", "cancelled"))
            total = 0
            for _ in range(rng.randint(1, 4)):
                product_id = rng.choice(product_ids)
                quantity = rng.randint(1, 3)
                total += prices[product_id] * quantity
                data["order_items"].append(
                    {
                        "id": next_id("order_items"),
                        "order_id": oid,
                        "product_id": product_id,
                        "quantity": quantity,
                        "unit_price_paise": prices[product_id],
                    }
                )
            data["orders"].append(
                {
                    "id": oid,
                    "customer_id": cid,
                    "address_id": address_id,
                    "status": status,
                    "total_paise": total,
                    "delivery_notes": (
                        f"Call {rng.choice(phone_variants(canary.mobile))} before delivery, "
                        f"leave with the watchman ({canary.token})"
                        if rng.random() < 0.4
                        else None
                    ),
                    "placed_at": placed,
                }
            )
            if status == "delivered":
                address = next(a for a in data["addresses"] if a["id"] == address_id)
                iid = next_id("invoices")
                data["invoices"].append(
                    {
                        "id": iid,
                        "order_id": oid,
                        "invoice_number": f"BB/INV/{iid:06d}",
                        "billing_name": f"{first} {last} {canary.token}",
                        "billing_address": (
                            f"{address['line1']}, {address['city']}, "
                            f"{address['state']} {address['pin_code']}, ref {canary.token}"
                        ),
                        "gstin": None,
                        "amount_paise": total,
                        "issued_at": placed + timedelta(days=rng.randint(2, 7)),
                    }
                )
            provider = rng.choice(("upi", "upi", "card", "cod"))
            data["payments"].append(
                {
                    "id": next_id("payments"),
                    "order_id": oid,
                    "provider": provider,
                    "provider_ref": (
                        None if provider == "cod" else f"pay_{rng.getrandbits(64):016x}X"
                    ),
                    "upi_id": (
                        f"{first.lower()}.{canary.token}@okaxis" if provider == "upi" else None
                    ),
                    "amount_paise": total,
                    "status": "failed" if status == "cancelled" else "captured",
                    "paid_at": None if status == "cancelled" else placed,
                }
            )

        for product_id in rng.sample(product_ids, rng.randint(0, 3)):
            data["reviews"].append(
                {
                    "id": next_id("reviews"),
                    "customer_id": cid,
                    "product_id": product_id,
                    "rating": rng.randint(1, 5),
                    "body": f"{fake.sentence()} - {first} ({canary.token})",
                    "created_at": ts(600),
                }
            )

        for _ in range(rng.randint(0, 2)):
            data["support_tickets"].append(
                {
                    "id": next_id("support_tickets"),
                    "customer_id": cid if rng.random() < 0.7 else None,  # else a guest ticket
                    "contact_email": canary.email,
                    "subject": rng.choice(("Where is my order?", "Refund request", "Wrong size")),
                    "body": (
                        f"Hi, this is {first} ({canary.token}). "
                        f"Please call me on {rng.choice(phone_variants(canary.mobile))}."
                    ),
                    "created_at": ts(400),
                }
            )

        for product_id in rng.sample(product_ids, rng.randint(0, 5)):
            data["wishlists"].append(
                {"customer_id": cid, "product_id": product_id, "added_at": ts(400)}
            )

        for _ in range(rng.randint(5, 40)):
            kind = rng.choice(("page_view", "page_view", "add_to_cart", "checkout", "login"))
            properties: dict[str, str] = {"path": rng.choice(("/", "/cart", "/p/kurta"))}
            if kind == "login":
                properties["email"] = canary.email
            data["events"].append(
                {
                    "id": next_id("events"),
                    "customer_id": cid,
                    "kind": kind,
                    "properties": properties,
                    "ip": fake.ipv4_public(),
                    "occurred_at": ts(365),
                }
            )

    return data


def load(conn: psycopg.Connection[Any], data: Dataset) -> dict[str, int]:
    """Recreate the schema and load the dataset in one transaction. Returns row counts."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
        cur.execute("CREATE SCHEMA public")
        cur.execute(sql.SQL(SCHEMA_SQL.read_text(encoding="utf-8")))
        for table in TABLES:
            rows = data[table]
            if not rows:
                continue
            columns = list(rows[0])
            copy_stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(
                sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, columns))
            )
            with cur.copy(copy_stmt) as copy:
                for row in rows:
                    copy.write_row([Jsonb(v) if isinstance(v, dict) else v for v in row.values()])
        for table in TABLES_WITH_IDENTITY:
            cur.execute(
                sql.SQL("SELECT setval(pg_get_serial_sequence({}, 'id'), max(id)) FROM {}").format(
                    sql.Literal(f"public.{table}"), sql.Identifier(table)
                )
            )
    return {table: len(data[table]) for table in TABLES}


def _unique_canaries(rng: random.Random, count: int) -> list[Canary]:
    canaries: list[Canary] = []
    tokens: set[str] = set()
    mobiles: set[str] = set()
    while len(canaries) < count:
        canary = Canary(uuid.UUID(int=rng.getrandbits(128), version=4))
        if canary.token in tokens or canary.mobile in mobiles:
            continue
        tokens.add(canary.token)
        mobiles.add(canary.mobile)
        canaries.append(canary)
    return canaries


def _pan(rng: random.Random, surname: str) -> str:
    """PAN-shaped value: AAA + P (individual) + surname initial + 4 digits + letter."""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    head = "".join(rng.choice(letters) for _ in range(3))
    initial = surname[0].upper() if surname[:1].isalpha() else "X"
    return f"{head}P{initial}{rng.randint(0, 9999):04d}{rng.choice(letters)}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--dsn", default=os.environ.get("VILOPA_APP_PG_DSN", DEFAULT_DSN))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--customers", type=int, default=200)
    parser.add_argument(
        "--allow-any-database",
        action="store_true",
        help=f"Load even if the database is not named {DEMO_DATABASE!r}. Drops schema public.",
    )
    args = parser.parse_args()

    data = generate(seed=args.seed, customers=args.customers)
    with psycopg.connect(args.dsn) as conn:
        current = conn.execute("SELECT current_database()").fetchone()
        name = current[0] if current else None
        if name != DEMO_DATABASE and not args.allow_any_database:
            raise SystemExit(
                f"refusing to seed database {name!r}: seeding drops schema public. "
                f"Use the {DEMO_DATABASE!r} database or pass --allow-any-database."
            )
        counts = load(conn, data)
    width = max(map(len, counts))
    for table, count in counts.items():
        print(f"{table:<{width}}  {count:>6}")
    print(f"seeded {name} with seed={args.seed}")


if __name__ == "__main__":
    main()
