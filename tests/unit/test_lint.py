from __future__ import annotations

from typing import Any

from mita.core import datamap
from mita.core.catalog import ColumnInfo
from mita.core.detect import ColumnReport, Confidence, classify
from mita.core.lint import Severity, lint


def make_map(extra_tables: dict[str, Any] | None = None, **top: Any) -> datamap.DataMap:
    return datamap.from_obj(
        {
            "version": 0,
            "connectors": [
                {"name": "app", "kind": "postgres", "dsn_env": "APP_DSN"},
                {"name": "crm", "kind": "postgres", "dsn_env": "CRM_DSN"},
            ],
            "principal": {"root": "app.public.users", "keys": {"email": {"column": "email"}}},
            "tables": {
                "app.public.users": {
                    "erase": "delete_row",
                    "columns": {"id": {"pii": "none"}, "email": {"pii": "email"}},
                },
                "crm.public.contacts": {"erase": "delete_row"},
                **(extra_tables or {}),
            },
            **top,
        }
    )


def live(*columns: tuple[str, str, str]) -> list[ColumnReport]:
    """Name-only reports for (table, column, data_type) triples in schema public."""
    out = []
    for table, name, data_type in columns:
        column = ColumnInfo("public", table, name, data_type)
        out.append(ColumnReport(column, 0, classify(column)))
    return out


USERS = (
    ("users", "id", "bigint"),
    ("users", "email", "text"),
    ("users", "created_at", "timestamp with time zone"),
)


def test_matching_map_and_database_is_clean() -> None:
    result = lint(make_map(), "app", live(*USERS))
    assert result.issues == []


def test_unmapped_personal_data_column_fails() -> None:
    result = lint(make_map(), "app", live(*USERS, ("users", "alt_mobile", "text")))
    assert [(i.severity, i.ref) for i in result.issues] == [
        (Severity.ERROR, "app.public.users.alt_mobile")
    ]
    assert "looks like phone (high" in result.issues[0].message


def test_unmapped_table_with_personal_data_fails() -> None:
    result = lint(make_map(), "app", live(*USERS, ("tickets", "contact_email", "text")))
    assert [i.ref for i in result.errors] == ["app.public.tickets.contact_email"]


def test_low_confidence_findings_are_counted_not_reported() -> None:
    reports = live(*USERS, ("products", "name", "text"))
    result = lint(make_map(), "app", reports)
    assert result.issues == []
    assert result.below_threshold == 1

    strict = lint(make_map(), "app", reports, fail_on=Confidence.LOW)
    assert [i.ref for i in strict.errors] == ["app.public.products.name"]


def test_stale_columns_and_tables_fail() -> None:
    dm = make_map({"app.public.orders": {"erase": "delete_row"}})
    result = lint(dm, "app", live(("users", "id", "bigint")))
    assert [(i.ref, i.message) for i in result.errors] == [
        ("app.public.users.email", "in the data map but not in the database"),
        ("app.public.orders", "in the data map but not in the database"),
    ]


def test_retention_and_match_columns_must_exist() -> None:
    dm = make_map(
        {
            "app.public.invoices": {
                "erase": "retain",
                "retain": {"reason": "Tax", "period": "P8Y", "from_column": "issued_at"},
            },
            "app.public.tickets": {
                "erase": "delete_row",
                "match": [{"key": "email", "column": "contact_email"}],
                "columns": {"contact_email": {"pii": "email"}},
            },
        }
    )
    reports = live(*USERS, ("invoices", "id", "bigint"), ("tickets", "id", "bigint"))
    result = lint(dm, "app", reports)
    assert [i.ref for i in result.errors] == [
        "app.public.invoices.issued_at",
        "app.public.tickets.contact_email",
    ]


def test_relation_columns_must_exist() -> None:
    dm = make_map(
        {"app.public.events": {"erase": "delete_row"}},
        relations=[{"from": "app.public.events.user_id", "to": "app.public.users.id"}],
    )
    result = lint(dm, "app", live(*USERS, ("events", "id", "bigint")))
    assert [i.ref for i in result.errors] == ["app.public.events.user_id"]


def test_reviewed_none_is_a_warning_not_an_error() -> None:
    dm = make_map(
        {"app.public.tickets": {"erase": "delete_row", "columns": {"phone": {"pii": "none"}}}}
    )
    result = lint(dm, "app", live(*USERS, ("tickets", "phone", "text")))
    assert result.errors == []
    assert [(i.severity, i.ref) for i in result.issues] == [
        (Severity.WARNING, "app.public.tickets.phone")
    ]


def test_other_connectors_are_ignored() -> None:
    result = lint(make_map(), "app", live(*USERS))
    assert all(not i.ref.startswith("crm.") for i in result.issues)
    # crm.public.contacts is not in this scan, and that is not app's problem.
    assert result.issues == []
