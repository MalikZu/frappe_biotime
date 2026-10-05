# frappe_biotime

Connects Frappe HR to ZKTeco BioTime.

> **Status: early development.** Nothing is released yet.
>
> This is an unofficial integration. It is not affiliated with or endorsed by ZKTeco.

## What works

- Import punches from one or more BioTime servers into Employee Checkin. See
  [Punch import](docs/punch-import.md).

## Planned

- Let Frappe HR's shift auto-attendance turn those punches into attendance.
- Push employees, with their department, branch and designation, to BioTime.
- A report that matches BioTime people to Frappe employees.
- Frappe HR v16 (`main`) and v15 (`version-15`).

Built on the [pybiotime](https://github.com/MalikZu/pybiotime) SDK.

## License

MIT
