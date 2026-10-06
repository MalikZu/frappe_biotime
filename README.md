# frappe_biotime

Connects Frappe HR to ZKTeco BioTime.

> **Status: early development.** Nothing is released yet.
>
> This is an unofficial integration. It is not affiliated with or endorsed by ZKTeco.

## What works

- Import punches from one or more BioTime servers into Employee Checkin. No punch is
  dropped: one that cannot be imported yet waits and is tried again.
- Move each Shift Type's Last Sync of Checkin only as far as every server has imported,
  so Frappe HR's auto attendance marks attendance without false Absents.
- Match BioTime people to Frappe employees in a report, and set their Attendance Device
  IDs from it.

See [Punch import](docs/punch-import.md) and [Employee mapping](docs/employee-mapping.md).

## Planned

- Push employees, with their department, branch and designation, to BioTime.
- Frappe HR v16 (`main`) and v15 (`version-15`).

Built on the [pybiotime](https://github.com/MalikZu/pybiotime) SDK.

## License

MIT
