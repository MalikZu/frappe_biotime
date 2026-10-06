# Employee mapping

The **BioTime Employee Mapping** report lists each person in BioTime next to the Frappe
employee with the same code, and sets **Attendance Device ID** for the ones that have none.
Use it before people's first punches, and when punches wait with Unknown device ID.

Open it from **Actions > Employee Mapping** on a BioTime Server. It needs the HR Manager or
System Manager role.

## What it shows

One row per code:

- **BioTime Code**, **BioTime Name** and **BioTime Department**, read from BioTime each
  time the report runs.
- **Waiting Punches**: punches waiting with this code as Unknown device ID. These rows
  come first.
- **Match** and **Employee**:

| Match | Meaning |
|---|---|
| Linked | This employee has the code as Attendance Device ID. |
| Same name | This employee has the same name, in any word order. |
| Similar name | One name is the other with more words, and they share at least two. |
| No match | No employee is suggested. |
| Not in BioTime | Punches wait with this code, but BioTime has no such person. |

Only employees without an Attendance Device ID are suggested. They must not be Left, and
must belong to the server's **Company** when it is set. Names match without case, accents,
or Arabic spelling variants. An employee who matches several codes is suggested for none
of them.

**Show** lists only the rows that need a match, or all of them.

## Link codes

1. Tick the rows.
2. Click **Link Codes**.

With several rows ticked, each suggested employee gets their row's code. With one row
ticked, you choose the employee, starting from the suggestion.

An employee who has an Attendance Device ID keeps it, and a code that another employee has
is not given twice. The report says which rows it could not link, and why. Linking needs
write access to the employee.

Punches waiting with a linked code import with the next import.

## When BioTime cannot be read

The report then lists only the codes with waiting punches, without BioTime names, and says
why. A server in Agent mode is never read from here.
