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
the employee, the server or the shift settings, and at least once a day. An Error is
tried again after about 10 minutes. **Import Now** tries all of them. A punch that
imports leaves the list.

| Reason | What to do |
|---|---|
| Unknown device ID | Set the employee's Attendance Device ID. **Unmapped Employee Codes** lists the codes. |
| Inactive employee | Set the employee to Active if the punch should count. |
| After relieving date | Correct the relieving date if it is wrong. |
| Log type required | Map punch states or set terminal directions. |
| No terminal coordinates | Add the terminal's **Latitude** and **Longitude**. |
| Outside check-in radius | Check the terminal's coordinates and the shift location. |
| Error | Read the punch's message and the Error Log. |
| Left out by settings | The server's settings changed after it started waiting. Undo the change if it should count. |

You can delete waiting punches you never want imported, such as visitors' codes.

## Punches that are not imported

Two kinds of new punches are counted in **Last Message** and not kept:

- **Already in Frappe:** the punch is already a checkin or waiting, or the employee
  already has a checkin at that exact second.
- **Left out by this server's settings:** the terminal's **Import Punches** is off, or
  the employee belongs to another company than the server's **Company**.

## Attendance

Frappe HR marks attendance, Absent included, for each Shift Type up to its **Last Sync of
Checkin**. Moving it before every punch is in Frappe would mark people Absent who were
there. So after each import, the app moves it forward on every Shift Type with **Enable
Auto Attendance** on and **Auto Update Last Sync** off, but only as far as every server
has imported:

- Each server's **Imported Up To** is the earliest of: the import's start; the last
  contact of each terminal that holds attendance, since an offline terminal may still
  have punches; the newest late punch of a terminal still uploading what it stored; and
  the oldest waiting punch that holds. **Held Back By** says which one.
- A refused punch or an Error holds attendance until it imports or you delete it. An
  unknown device ID holds only while it is new: for **Hold for New Unknown Codes** (24
  hours by default), and not at all for a code that has waited longer, such as a
  visitor's. Inactive, After relieving date and Left out by settings never hold.
- Last Sync of Checkin then moves to the earliest Imported Up To of all servers, minus
  **Attendance Buffer** (60 minutes by default). It never moves back. A disabled server
  keeps holding at its last Imported Up To, because its punches may still come: untick
  its **Import Punches** to release it. Agent mode servers do not count yet.

**Before attendance starts, set two fields on each Shift Type yourself:** first **Process
Attendance After**, to **Import From** or later, then **Last Sync of Checkin**. Frappe HR
marks Absent on every day from Process Attendance After that has no attendance, so an
earlier date marks days before the import began Absent. With several servers, use the
latest Import From. The app never starts a Shift Type, and leaves alone any it cannot
move safely. The server form lists them.

A terminal that is gone for good holds attendance until you untick its **Holds
Attendance**. Do not untick Import Punches for that: its punches would then be left out,
even ones it uploads if it comes back.

## After a BioTime restore

When BioTime's database is restored from a backup or reinstalled, it gives new punches
transaction ids it used before. **Run Start Over after any restore.** The import notices
a restore only when the ids go back far, as after a reinstall, and then fails with a
message that says so. After a smaller restore, it cannot tell, and new punches that
reuse old ids are missed until you run Start Over.

1. Click **Actions > Start Over**.
2. Pick the date to read BioTime again from: the day the restore lost data, or a little
   earlier. It starts at today minus **Lookback**. Reading many months again can take
   longer than one import may run.

Imports then read again from that date. Punches already in Frappe are recognized by
their time and are not imported twice. **Key Generation** in the Status section goes
up by one, so new punches never match old ones.

## When an import fails

An import fails only when BioTime cannot be read or the database fails. **Last
Result** shows Failed, **Last Message** shows why, and an Error Log is saved. The
next import reads the same punches again, so none are lost.

One import must finish within the long queue's job timeout, 25 minutes by default.
For the first import, keep **Import From** recent.
