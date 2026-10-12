from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from mita.core import datamap
from mita.core.datamap import DataMapError, TableErase

MINIMAL: dict[str, Any] = {
    "version": 0,
    "connectors": [{"name": "app", "kind": "postgres", "dsn_env": "APP_DSN"}],
    "principal": {
        "root": "app.public.users",
        "keys": {"email": {"column": "email", "normalize": "email"}},
    },
    "tables": {
        "app.public.users": {"erase": "delete_row", "columns": {"email": {"pii": "email"}}},
    },
}


def with_changes(**tables: Any) -> dict[str, Any]:
    raw = copy.deepcopy(MINIMAL)
    raw["tables"].update(tables)
    return raw


def messages(raw: object) -> list[str]:
    with pytest.raises(DataMapError) as info:
        datamap.from_obj(raw)
    return [str(p) for p in info.value.problems]


def test_minimal_map_is_valid() -> None:
    dm = datamap.from_obj(MINIMAL)
    assert dm.tables["app.public.users"].erase is TableErase.DELETE_ROW


def test_demo_map_is_valid(demo_datamap_path: Path) -> None:
    dm = datamap.load(demo_datamap_path)
    assert dm.principal.root == "app_pg.public.customers"
    assert len(dm.tables) == 9


def test_unknown_keys_are_rejected() -> None:
    raw = copy.deepcopy(MINIMAL)
    raw["tables"]["app.public.users"]["strategy"] = "delete_row"
    assert any("Extra inputs are not permitted" in m for m in messages(raw))


def test_error_paths_quote_dotted_table_names() -> None:
    raw = with_changes(**{"app.public.orders": {"erase": "shred"}})
    assert any(m.startswith("tables['app.public.orders'].erase:") for m in messages(raw))


@pytest.mark.parametrize(
    ("tables", "expected"),
    [
        pytest.param(
            {"app.public.invoices": {"erase": "retain"}},
            "erase: retain needs a retain block",
            id="table-retain-without-reason",
        ),
        pytest.param(
            {
                "app.public.orders": {
                    "erase": "delete_row",
                    "retain": {"reason": "tax", "period": "P6Y"},
                }
            },
            "a table retain block needs erase: retain",
            id="retain-block-on-delete-table",
        ),
        pytest.param(
            {
                "app.public.orders": {
                    "erase": "update_columns",
                    "columns": {"notes": {"pii": "free_text"}},
                }
            },
            "free_text column needs a strategy",
            id="update-columns-pii-without-strategy",
        ),
        pytest.param(
            {"app.public.orders": {"erase": "update_columns", "columns": {}}},
            "update_columns table has no column with a strategy",
            id="update-columns-without-any-strategy",
        ),
        pytest.param(
            {
                "app.public.orders": {
                    "erase": "delete_row",
                    "columns": {"notes": {"pii": "free_text", "strategy": "nullify"}},
                }
            },
            "strategy only applies to update_columns tables",
            id="strategy-on-delete-table",
        ),
        pytest.param(
            {
                "app.public.payments": {
                    "erase": "update_columns",
                    "columns": {"ref": {"pii": "payment_ref", "strategy": "retain"}},
                }
            },
            "strategy: retain needs a retain block",
            id="column-retain-without-reason",
        ),
        pytest.param(
            {
                "app.public.payments": {
                    "erase": "update_columns",
                    "columns": {
                        "ref": {
                            "pii": "payment_ref",
                            "strategy": "nullify",
                            "retain": {"reason": "x", "period": "P1Y"},
                        }
                    },
                }
            },
            "a retain block needs strategy: retain",
            id="column-retain-block-without-retain-strategy",
        ),
        pytest.param(
            {"other.public.logs": {"erase": "delete_row"}},
            "connector 'other' is not declared",
            id="undeclared-connector",
        ),
        pytest.param(
            {
                "app.public.tickets": {
                    "erase": "delete_row",
                    "match": [{"key": "phone", "column": "contact"}],
                    "columns": {"contact": {"pii": "phone"}},
                }
            },
            "key 'phone' is not declared in principal.keys",
            id="match-unknown-key",
        ),
        pytest.param(
            {
                "app.public.tickets": {
                    "erase": "delete_row",
                    "match": [{"key": "email", "column": "contact_email"}],
                }
            },
            "column 'contact_email' is not listed",
            id="match-unlisted-column",
        ),
    ],
)
def test_semantic_problems(tables: dict[str, Any], expected: str) -> None:
    assert any(expected in m for m in messages(with_changes(**tables)))


def test_retain_reason_cannot_be_blank() -> None:
    raw = with_changes(
        **{"app.public.invoices": {"erase": "retain", "retain": {"reason": "  ", "period": "P6Y"}}}
    )
    assert any("retain.reason" in m for m in messages(raw))


@pytest.mark.parametrize("period", ["P", "6Y", "P6", "PT6H", "P-1Y"])
def test_retain_period_must_be_iso_date_period(period: str) -> None:
    raw = with_changes(
        **{"app.public.invoices": {"erase": "retain", "retain": {"reason": "x", "period": period}}}
    )
    assert any("retain.period" in m for m in messages(raw))


def test_root_must_be_declared_and_erasable() -> None:
    raw = copy.deepcopy(MINIMAL)
    raw["principal"]["root"] = "app.public.people"
    assert any("principal.root" in m for m in messages(raw))

    raw = copy.deepcopy(MINIMAL)
    raw["tables"]["app.public.users"] = {
        "erase": "retain",
        "retain": {"reason": "x", "period": "P1Y"},
        "columns": {"email": {"pii": "email"}},
    }
    assert any("cannot use erase: retain" in m for m in messages(raw))


def test_principal_key_column_must_be_listed_on_root() -> None:
    raw = copy.deepcopy(MINIMAL)
    raw["principal"]["keys"]["phone"] = {"column": "mobile", "normalize": "in_mobile"}
    assert any("principal.keys.phone.column" in m for m in messages(raw))


def test_duplicate_connectors_are_reported() -> None:
    raw = copy.deepcopy(MINIMAL)
    raw["connectors"].append(dict(raw["connectors"][0]))
    assert any("duplicate connector 'app'" in m for m in messages(raw))


def test_relations_must_reference_declared_tables() -> None:
    raw = copy.deepcopy(MINIMAL)
    raw["relations"] = [
        {"from": "app.public.events.user_id", "to": "app.public.users.id"},
        {"from": "app.public.users.id", "to": "app.public.users.id"},
    ]
    found = messages(raw)
    assert any("relations[0].from" in m and "app.public.events" in m for m in found)
    assert any("cannot point at itself" in m for m in found)


def test_all_problems_are_reported_together() -> None:
    raw = with_changes(
        **{
            "app.public.invoices": {"erase": "retain"},
            "other.public.logs": {"erase": "delete_row"},
        }
    )
    assert len(messages(raw)) == 2


def test_duplicate_yaml_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "datamap.yaml"
    path.write_text(
        "version: 0\ntables:\n  a.b.c: {erase: delete_row}\n  a.b.c: {erase: retain}\n",
        encoding="utf-8",
    )
    with pytest.raises(DataMapError) as info:
        datamap.load(path)
    assert "duplicate key 'a.b.c'" in str(info.value.problems[0])


def test_invalid_yaml_is_a_problem_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "datamap.yaml"
    path.write_text("version: [0\n", encoding="utf-8")
    with pytest.raises(DataMapError) as info:
        datamap.load(path)
    assert "invalid YAML" in str(info.value.problems[0])


def test_hostile_identifiers_are_accepted_as_data() -> None:
    # Names are passed to Postgres only through psycopg.sql.Identifier, so a hostile
    # name is harmless and must not be "cleaned" into a different table.
    raw = with_changes(**{'app.public.x";DROP_TABLE_y': {"erase": "delete_row"}})
    assert 'app.public.x";DROP_TABLE_y' in datamap.from_obj(raw).tables
