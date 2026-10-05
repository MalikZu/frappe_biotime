# Punch import

The app reads punches from BioTime and saves each one as an Employee Checkin.
Frappe HR's shift auto-attendance then turns checkins into attendance.

## Before you start

- Every employee who punches needs **Attendance Device ID (Biometric/RF tag ID)**
  set to their employee code in BioTime.
- You need a BioTime user that the app can sign in with.

## Set up a server

1. Create a **BioTime Server**.
2. Fill in **URL**, **Authentication**, **Username** and **Password**. Most servers
   use Token authentication.
3. Set **BioTime Timezone** only when BioTime runs in a different timezone than
   this site.
4. Set **Company** to import only that company's employees. Leave it empty for all.
5. Set **Import From** to the first date you want imported, then save.
6. Click **Actions > Test Connection**. It shows the BioTime version.
7. Click **Actions > Sync Terminals** to list the terminals.

To leave a terminal out, untick its **Import Punches**.

## Log type

**Log Type** decides whether a checkin is IN or OUT:

- **Leave blank** (default): the shift type alternates IN and OUT. Use this when
  terminals send the same punch state for every punch.
- **Map punch states**: states listed in **IN Punch States** become IN, states in
  **OUT Punch States** become OUT, and others stay blank.
- **Terminal direction**: each punch takes its terminal's **Direction**.

If a shift type is set to "Strictly based on Log Type in Employee Checkin", do not
leave the log type blank. The form warns you when it is.

## How imports run

- Every 4 minutes, each enabled server with **Mode** set to Pull and **Import
  Punches** on is queued for an import. **Actions > Import Now** queues one at once.
- Each import reads the punches that reached BioTime since the last import. This
  includes old punches that an offline terminal uploads late.
- Each punch is imported once. The checkin's BioTime section shows where it came from.
- The **Status** section shows the last import's time, result, count and message.

## Waiting punches

No punch is dropped. A punch that cannot become a checkin yet waits, with its reason.
**Actions > Waiting Punches** lists them, and the form says how many there are.

An import tries a waiting punch again when something it depends on changes, such as
the employee, the server or the shift settings, and at least once a day. **Import
Now** tries all of them. A punch that imports leaves the list.

| Reason | What to do |
|---|---|
| Unknown device ID | Set the employee's Attendance Device ID. **Unmapped Employee Codes** lists the codes. |
| Inactive employee | Set the employee to Active if the punch should count. |
| After relieving date | Correct the relieving date if it is wrong. |
| Log type required | Map punch states or set terminal directions. |
| No terminal coordinates | Add the terminal's **Latitude** and **Longitude**. |
| Outside check-in radius | Check the terminal's coordinates and the shift location. |
| Error | Read the punch's message and the Error Log. |

You can delete waiting punches you never want imported, such as visitors' codes.

## Punches that are not imported

Two kinds are counted in **Last Message** and not kept:

- **Already in Frappe:** the punch is already a checkin, or the employee already has a
  checkin at that exact second.
- **Left out by this server's settings:** the terminal's **Import Punches** is off, or
  the employee belongs to another company than the server's **Company**.

## When an import fails

An import fails only when BioTime cannot be read or the database fails. **Last
Result** shows Failed, **Last Message** shows why, and an Error Log is saved. The
next import reads the same punches again, so none are lost.

One import must finish within the long queue's job timeout, 25 minutes by default.
For the first import, keep **Import From** recent.
