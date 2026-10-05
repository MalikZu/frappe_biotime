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

## Skipped punches

Some punches cannot become checkins. They are skipped and counted in **Last Message**:

| Reason | What to do |
|---|---|
| No employee with that attendance device ID | Set the employee's Attendance Device ID. The codes are listed in **Unmapped Employee Codes**. |
| Employee of another company | Nothing, or clear the server's **Company**. |
| Terminal not imported | Tick the terminal's **Import Punches**. |
| Employee inactive, or punch after the relieving date | Nothing. |
| Already imported, or same time already logged | Nothing. The checkin exists. |
| No log type in a shift that needs one | Map punch states or set terminal directions. |
| Terminal has no coordinates and geolocation tracking is on | Add the terminal's **Latitude** and **Longitude**. |
| Outside the shift location's check-in radius | Check the terminal's coordinates and the shift location. |

**Skipped punches are not retried.** They stay in BioTime, but a later import does
not read them again. Set Attendance Device IDs before new employees start punching.

## When an import fails

**Last Result** shows Failed, **Last Message** shows why, and an Error Log is saved.
The next import reads the same punches again, so none are lost.

One import must finish within the long queue's job timeout, 25 minutes by default.
For the first import, keep **Import From** recent.
