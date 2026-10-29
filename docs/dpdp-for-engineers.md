# DPDP for engineers

> **Owner: Hrithik. Write this yourself, in your own words, from the official Rules PDF.**
> It is the page you will be questioned on. Do not paste summaries from blogs or AI tools.
> Link the exact rule for every claim. Nothing here is legal advice.

Source to read: the Digital Personal Data Protection Rules, 2025, as notified in the Gazette
of India. Read Rules 8, 13 and 14 and the Third Schedule first.

## Questions this page must answer

1. When do the erasure, access and retention duties start? (Blog sources disagree on
   13 vs 14 May 2027. Quote the gazette.)
2. What is the deadline for answering an erasure or access request, and when does the clock
   start?
3. When must data be erased without anyone asking (purpose served, period of inactivity)?
   Which classes of business have fixed periods?
4. What advance notice must a person get before retention-based erasure, and through what
   channel?
5. What may or must be kept despite an erasure request (other laws, such as tax records)?
6. What must a company be able to show afterwards? What counts as evidence?
7. How must a company verify the identity of the person making the request?

## How each answer maps to vilopa

| Duty | vilopa mechanism | Status |
| --- | --- | --- |
| Answer erasure/access within the deadline | `due_at` computed at intake, SLA metrics | planned (week 9) |
| Erase when purpose is served | retention sweeps with advance notice | planned (week 9) |
| Keep what other laws require | `retain` strategy with reason, period and rule reference | data map v0 (week 1) |
| Show it happened | verification scan and evidence records | planned (week 4) |
