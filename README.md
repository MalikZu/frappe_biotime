# frappe_biotime

Connects Frappe HR to ZKTeco BioTime.

> **Status: early development.** Nothing is released yet.
>
> This is an unofficial integration. It is not affiliated with or endorsed by ZKTeco.

## What works

- Import punches from one or more BioTime servers into Employee Checkin. No punch is
  dropped: one that cannot be imported yet waits and is tried again.
- Re-import chosen days, for everyone or some employees, to bring in punches that were
  left out or never read.
- Move each Shift Type's Last Sync of Checkin only as far as every server has imported,
  so Frappe HR's auto attendance marks attendance without false Absents.
- Match BioTime people to Frappe employees in a report, and set their Attendance Device
  IDs from it.
- Push employees, with their department, branch and designation, to BioTime. Frappe is
  the master for employee records.

See [Punch import](docs/punch-import.md), [Employee mapping](docs/employee-mapping.md) and
[Employee push](docs/employee-push.md).

## Planned

- Frappe HR v16 (`main`) and v15 (`version-15`).

Built on the [pybiotime](https://github.com/MalikZu/pybiotime) SDK.

## License

MIT
