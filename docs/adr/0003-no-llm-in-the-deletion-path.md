# 0003. No language model in the deletion path

- Status: accepted
- Date: 2026-09-30

## Context

Every core job in mita (resolving a person across tables, ordering deletes, calling APIs,
verifying) has a correct, deterministic answer. A deletion tool that is "usually right"
destroys the wrong customer's data some of the time.

Sending customer records to a hosted model API in order to delete customer records is also
a new transfer of personal data to a new processor.

## Decision

No LLM ships in v1. Planning, execution and verification stay deterministic and tested.

The one place a model may later earn a role is flagging PII inside free-text columns during
`mita scan`. If it ever ships, it must:

- run locally, never through a hosted API;
- beat a rules-plus-NER baseline (Microsoft Presidio with Indian recognisers) on a published
  evaluation set, on recall at equal or better precision;
- only **add** "review this column" flags. It can never remove a deterministic flag, edit the
  data map or trigger an erasure;
- have every returned span checked against the exact input characters, dropping any that
  do not match.

## Consequences

- Behaviour is reproducible and testable with ordinary property and system tests.
- Free-text PII detection in v1 is limited to regex and validators. `docs/limitations.md`
  must say so.
- Prompt injection inside customer data cannot cause deletion, because model output has no
  authority.
