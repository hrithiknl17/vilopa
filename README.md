# mita

Delete a person's data from every system you mapped, prove it, and hit the deadline.
Self-hosted. DPDP-aware. Not legal advice.

> **Status: pre-alpha (week 1).** mita does not delete anything yet. What exists today: the
> data map format with a validator, and a demo shop database seeded with canary data for
> testing erasure. Follow the [roadmap](#roadmap).

## Why this exists

- From May 2027, Indian companies must answer erasure and access requests within a fixed
  deadline and delete personal data once its purpose is served (DPDP Act and Rules).
- Personal data spreads across your database, file storage and SaaS tools. A hand-written
  delete script does not verify, does not track deadlines, and silently breaks when someone
  adds a new column holding a phone number.

## What it is not

- Not a consent manager (see [tsi-dpdp-cms](https://github.com/tsi-coop/tsi-dpdp-cms)).
- Not legal advice, and not "compliance in a box". Companies are compliant; tools are not.
- It cannot see copies you did not map, and it cannot rewrite old backups.

## The data map

You describe where personal data lives in a YAML file kept in your repo and reviewed in PRs:

```yaml
principal:
  root: app_pg.public.customers
  keys:
    email: { column: email, normalize: email }

tables:
  app_pg.public.invoices:
    erase: retain
    retain:
      reason: Tax invoices must be kept as issued.
      period: P8Y
      from_column: issued_at
```

See the full demo map in [`examples/bharat-bazaar/datamap.yaml`](examples/bharat-bazaar/datamap.yaml)
and the JSON Schema in [`schemas/datamap.schema.json`](schemas/datamap.schema.json). Editors with the
YAML language server get completion and validation from it.

```console
$ uv run mita datamap validate examples/bharat-bazaar/datamap.yaml
examples/bharat-bazaar/datamap.yaml: ok (9 tables, 1 connector(s))
```

## Development

Needs [uv](https://docs.astral.sh/uv/) and Docker.

```sh
make dev        # uv sync + start Postgres 16 on 127.0.0.1:55432
make seed       # load the Bharat Bazaar demo shop
make check      # ruff, mypy --strict, unit tests
make test-db    # integration tests against the dev database
```

No `make` (for example on Windows)? The same steps:

```sh
uv sync
docker compose -f deploy/compose.dev.yml up -d --wait
uv run python examples/bharat-bazaar/seed.py
uv run ruff check . && uv run mypy && uv run pytest -m "not db"
MITA_APP_PG_DSN=postgresql://mita:mita@127.0.0.1:55432/bharat_bazaar uv run pytest -m db
```

### The demo shop and its canaries

`examples/bharat-bazaar` is a small Indian e-commerce schema with shapes that make erasure
hard on purpose: a self-referencing FK, orders kept for accounting, tax invoices that must be
retained, guest support tickets linked only by email, an analytics table with no foreign key,
and phone numbers inside free text in four different formats.

Every seeded customer carries a unique canary token in their personal fields. The erasure
tests will check two things: after erasing one customer, their token appears nowhere
(**canary**), and every other customer's rows are unchanged (**bystander**). The seed is
deterministic, so failures reproduce.

## Architecture decisions

- [0001 Postgres is the job queue](docs/adr/0001-postgres-as-the-job-queue.md)
- [0002 Principals are stored as keyed hashes](docs/adr/0002-hmac-principals.md)
- [0003 No language model in the deletion path](docs/adr/0003-no-llm-in-the-deletion-path.md)

## Roadmap

1. **`mita scan` and `mita lint`**: find PII-looking columns in a live Postgres schema, and
   fail CI when a migration adds one the data map does not cover. Ships as a GitHub Action.
2. **`mita plan`**: resolve a person across tables through foreign keys and declared
   relations, order the deletes, and refuse plans that touch too many rows.
3. **`mita erase` / `mita access`**: execute, verify, and write an evidence record.
4. **Service**: request intake, approvals, deadlines, retention sweeps, connectors.

## License

[Apache-2.0](LICENSE)
