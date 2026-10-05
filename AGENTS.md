# frappe_biotime

Frappe app that connects Frappe HR to ZKTeco BioTime servers. It imports punches
into Employee Checkin and can push employees to BioTime. All BioTime calls go
through the `pybiotime` SDK.

**Maintainers: read `internal/README.md` at the start of every session.**
It holds the current status, decisions and next step. `internal/` is not in git.

## Rules

- **Frappe HR first.** Use its native features before inventing a parallel one:
  `Employee Checkin` for punches, `Employee.attendance_device_id` for the
  BioTime `emp_code`, Shift Type auto-attendance for IN/OUT and shift boundaries,
  standard Department, Branch and Designation for masters.
- **Few doctypes.** The design has two doctypes (BioTime Server, and BioTime
  Pending Punch for punches that wait) and one child table (BioTime Terminal).
  A new doctype needs a written reason in the design doc first.
- **Never lose or invent attendance.** Imports must catch late uploads and stay
  idempotent. Never move `Shift Type.last_sync_of_checkin` past what every
  server has fully imported; that creates false Absents.
- **No raw HTTP.** Every BioTime request goes through `pybiotime`. If the SDK
  lacks something, add it to the SDK.
- **Never block a user's save on sync.** Enqueue after commit with a `job_id`
  and deduplication. Never throw because a sync job is pending.
- **Hook only what we sync.** No `doc_events["*"]`.
- **Secrets** live in `Password` fields and are read with `get_password()` inside
  jobs. Never log tokens or passwords.
- **No network in tests.** Fake BioTime with `pybiotime.testing`.

## Branches — there are two

`main` targets **Frappe v16 / Python 3.14**. `version-15` targets **Frappe v15 /
Python 3.11**. A fix on `main` is not done until it is ported to `version-15`.
Decide port / do-not-port / port-differently before writing it. `version-15`
cannot use PEP 695 `type` aliases, PEP 758 `except A, B:`, or v16-only Frappe APIs.

## Working on this repo

- `internal/` is never committed. No internal plans or notes in commits, docs or
  code comments.
- Commit as you go, one small commit per coherent change. Conventional commits:
  `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `ci`, `perf`.
- Frappe's official skills help here. Install locally:
  `npx skills add frappe/skills --skill frappe-app-dev --skill quality-code-review -a claude-code -y`
  They have no upstream license, so never commit them.
- Docs are for users: short sentences, plain words, no filler. A change a user
  can see ships its doc in the same PR.
- Public text never credits individuals by name and never names customers.
- Keep this file small. Details go in `docs/` and get linked.
