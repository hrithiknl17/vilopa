"""The estimator against an in-memory copy of the seeded demo shop.

Expected counts are computed straight from the generated rows, independently of the plan,
so a wrong resolve path or a lost guest ticket shows up as a count mismatch.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from vilopa.core import datamap
from vilopa.core.catalog import Schema
from vilopa.core.plan import Plan, StepKind, build, check_blast, normalize, parse_principal
from vilopa.engine.estimate import Condition, KeyCondition, Row, estimate

Data = dict[str, list[dict[str, Any]]]


class MemorySource:
    """`Source` over the seed module's Dataset, with the connector's text semantics."""

    def __init__(self, data: Data) -> None:
        self.data = data

    def select(
        self, table_ref: str, columns: Sequence[str], conditions: Sequence[Condition]
    ) -> list[Row]:
        rows = self.data[table_ref.rsplit(".", 1)[1]]
        return [
            tuple(_text(row.get(c)) for c in columns)
            for row in rows
            if any(_matches(row, c) for c in conditions)
        ]

    def total(self, table_ref: str) -> int:
        return len(self.data[table_ref.rsplit(".", 1)[1]])


def _matches(row: dict[str, Any], condition: Condition) -> bool:
    if isinstance(condition, KeyCondition):
        value = _text(row.get(condition.column))
        return value is not None and normalize(value, condition.normalize) in condition.values
    return tuple(_text(row.get(c)) for c in condition.columns) in condition.values


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


@pytest.fixture(scope="module")
def dataset(seed_module: ModuleType) -> Data:
    data: Data = seed_module.generate(seed=42, customers=50)
    return data


@pytest.fixture(scope="module")
def demo(demo_datamap_path: Path, demo_schema: Schema) -> tuple[datamap.DataMap, Plan]:
    dm = datamap.load(demo_datamap_path)
    return dm, build(dm, {"app_pg": demo_schema})


def expected(data: Data, customer: dict[str, Any]) -> dict[str, int]:
    cid, email = customer["id"], customer["email"]
    orders = {o["id"] for o in data["orders"] if o["customer_id"] == cid}
    by_customer = {
        t: sum(1 for r in data[t] if r["customer_id"] == cid)
        for t in ("addresses", "reviews", "wishlists", "events")
    }
    tickets = sum(
        1
        for r in data["support_tickets"]
        if r["customer_id"] == cid or r["contact_email"].lower() == email.lower()
    )
    return {
        "customers": 1,
        **by_customer,
        "orders": len(orders),
        "invoices": sum(1 for r in data["invoices"] if r["order_id"] in orders),
        "payments": sum(1 for r in data["payments"] if r["order_id"] in orders),
        "support_tickets": tickets,
    }


def short(counts: dict[str, int]) -> dict[str, int]:
    return {k.removeprefix("app_pg.public."): v for k, v in counts.items()}


@pytest.mark.parametrize("key", ["email", "phone", "user_id"])
def test_counts_match_the_data_for_every_identifier(
    dataset: Data, demo: tuple[datamap.DataMap, Plan], key: str
) -> None:
    dm, plan = demo
    # A customer with a guest ticket, which only the email match can find.
    guests = {t["contact_email"] for t in dataset["support_tickets"] if not t["customer_id"]}
    customer = next(c for c in dataset["customers"] if c["email"] in guests)
    value = {
        "email": customer["email"].upper(),
        "phone": customer["phone"],
        "user_id": customer["id"],
    }[key]

    got = estimate(plan, parse_principal(dm, [f"{key}:{value}"]), {"app_pg": MemorySource(dataset)})

    assert short(got.targets) == expected(dataset, customer)
    assert got.totals["app_pg.public.events"] == len(dataset["events"])
    assert check_blast(plan, got) == []


def test_detach_counts_people_the_principal_referred(
    dataset: Data, demo: tuple[datamap.DataMap, Plan]
) -> None:
    dm, plan = demo
    referrers = [c["referred_by_customer_id"] for c in dataset["customers"]]
    customer = next(c for c in dataset["customers"] if c["id"] in referrers)
    principal = parse_principal(dm, [f"user_id:{customer['id']}"])
    got = estimate(plan, principal, {"app_pg": MemorySource(dataset)})
    detach = next(
        i
        for i, s in enumerate(plan.steps)
        if s.kind is StepKind.DETACH and s.table.endswith(".customers")
    )
    assert got.rows(plan, detach) == referrers.count(customer["id"]) > 0


def test_unknown_person_touches_nothing(dataset: Data, demo: tuple[datamap.DataMap, Plan]) -> None:
    dm, plan = demo
    principal = parse_principal(dm, ["email:nobody@example.test"])
    got = estimate(plan, principal, {"app_pg": MemorySource(dataset)})
    assert set(got.targets.values()) == {0}
    assert all(got.rows(plan, i) == 0 for i in range(len(plan.steps)))


def test_identifiers_of_two_people_trip_the_guard(
    dataset: Data, demo: tuple[datamap.DataMap, Plan]
) -> None:
    dm, plan = demo
    a, b = dataset["customers"][:2]
    principal = parse_principal(dm, [f"email:{a['email']}", f"phone:{b['phone']}"])
    got = estimate(plan, principal, {"app_pg": MemorySource(dataset)})
    assert got.targets["app_pg.public.customers"] == 2
    assert any("match 2 rows" in p.message for p in check_blast(plan, got))
