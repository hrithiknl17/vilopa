# vilopa

Delete a person's data from every system you mapped, prove it, and hit the deadline.
Self-hosted. DPDP-aware. Not legal advice.

> **Status: pre-alpha.** vilopa does not delete anything yet. What exists today: the data map
> format with a validator, `vilopa scan` and `vilopa lint` for finding personal data your map
> misses, and a demo shop database seeded with canary data for testing erasure. Follow the
> [roadmap](#roadmap).

![vilopa lint catching an undocumented phone column](https://raw.githubusercontent.com/hrithiknl17/vilopa/main/docs/demo.gif)

## Install

```sh
pip install vilopa    # or: uv tool install vilopa
```

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
$ uv run vilopa datamap validate examples/bharat-bazaar/datamap.yaml
examples/bharat-bazaar/datamap.yaml: ok (9 tables, 1 connector(s))
```

## Finding personal data: `vilopa scan` and `vilopa lint`

`vilopa scan` lists the columns of a live Postgres database that look like personal data, from
their names, types and a sample of their values. It recognises Indian identifiers by shape:
mobile numbers in any common format, PAN, GSTIN, Aadhaar (with its check digit), UPI IDs and
PIN codes, as well as emails, IP addresses, and free text with emails or phone numbers inside.

```console
$ DATABASE_URL=postgresql://... uv run vilopa scan --connector app_pg
COLUMN                                       CATEGORY     CONFIDENCE  WHY
app_pg.public.customers.email                email        high        name suggests email; 200/200 sampled values look like email
app_pg.public.customers.pan                  pan          high        name suggests pan; 94/94 sampled values look like pan
app_pg.public.events.properties              free_text    medium      36/200 sampled values contain an email address or phone number
app_pg.public.events.ip                      ip_address   high        column type is inet
app_pg.public.products.name                  name         low         name suggests name
...
71 columns scanned, 22 look like personal data (14 high, 6 medium, 2 low)
```

`vilopa lint` fails when the database and the map disagree: a column that looks like personal
data at medium confidence or above is not in the map, or the map names a table or column that
no longer exists. Columns you reviewed and marked `pii: none` never fail the lint.

```console
$ uv run vilopa lint examples/bharat-bazaar/datamap.yaml
examples/bharat-bazaar/datamap.yaml: error: app_pg.public.customers.alt_mobile: looks like phone (high: name suggests phone); not in the data map
examples/bharat-bazaar/datamap.yaml: app_pg: 1 error(s), 0 warning(s)
```

Both connect read-only with a statement timeout, and never print, log or store a sampled
value: reports carry categories, confidences and counts only. Sampling reads the first rows a
table returns, not a random sample; `--sample-rows 0` classifies by name and type alone, which
is what you want against a freshly migrated, empty CI database.

### In CI

Run your migrations against a throwaway database, then lint the map in the same job:

```yaml
- uses: hrithiknl17/vilopa@v0.0.1  # pin a commit SHA in real use
  env:
    VILOPA_APP_PG_DSN: postgresql://app:app@localhost:5432/app  # the map's dsn_env
  with:
    datamap: privacy/datamap.yaml
    sample-rows: 0
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
VILOPA_APP_PG_DSN=postgresql://vilopa:vilopa@127.0.0.1:55432/bharat_bazaar uv run pytest -m db
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

1. ~~**`vilopa scan` and `vilopa lint`**~~ (done): find PII-looking columns in a live Postgres
   schema, and fail CI when a migration adds one the data map does not cover. Ships as a
   GitHub Action.
2. **`vilopa plan`**: resolve a person across tables through foreign keys and declared
   relations, order the deletes, and refuse plans that touch too many rows.
3. **`vilopa erase` / `vilopa access`**: execute, verify, and write an evidence record.
4. **Service**: request intake, approvals, deadlines, retention sweeps, connectors.

## License

[Apache-2.0](LICENSE)
