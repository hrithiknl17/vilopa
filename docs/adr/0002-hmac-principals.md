# 0002. Principals are stored as keyed hashes

- Status: accepted
- Date: 2026-09-30

## Context

vilopa deletes personal data. If it stores the email or phone number of everyone who asked
for erasure, vilopa itself becomes a store of exactly the data it was asked to remove, and a
list of "people who asked to be forgotten" is sensitive in its own right.

vilopa still needs to find open requests for the same person, enforce "one open erasure per
person", and match retention notices.

## Decision

- Store each principal as `HMAC-SHA256(pepper, normalised identifier)`. The pepper lives
  outside the database (environment variable or KMS) and carries a key id so it can rotate.
- Keep the raw identifier encrypted only while a request is open; set it to NULL when the
  request closes.
- Evidence records hold counts and hashes, never values.

## Consequences

- Lookups stay exact and indexable. Uniqueness constraints work on the hash.
- Emails and phone numbers are low-entropy. If the pepper leaks, an attacker can
  dictionary-attack the hashes. The pepper must be treated like a signing key.
- Normalisation must be deterministic and versioned (lower-cased email, E.164 phone), or the
  same person hashes to two values.
- Rotation means re-hashing open requests while their encrypted identifiers still exist;
  closed requests keep their old key id.

## Rejected

- **Plain identifiers:** turns vilopa into a PII store.
- **Unkeyed SHA-256:** trivially reversible for emails and Indian mobile numbers by brute force.
