# Employee push

Frappe is the master for employee records. With **Push Employees** on, the app keeps each
employee's copy in BioTime up to date. Punches still come from BioTime: see
[Punch import](punch-import.md).

## Turn it on

1. On the BioTime Server, open **Employee Push** and tick **Push Employees**. The server
   must be in Pull mode.
2. Choose where BioTime's department, area and position come from:
   - **BioTime Department From**: the employee's Department, or a fixed code.
   - **BioTime Area From**: the employee's Branch, or a fixed code.
   - **BioTime Position From**: the employee's Designation, or None to leave BioTime's
     as it is.
3. BioTime needs a department and an area for everyone. **Default Department Code** and
   **Default Area Code** name ones that already exist in BioTime. They are used for a fixed
   code, and for employees without a Department or Branch.
4. Choose **On Employee Left**.
5. Save, then click **Actions > Push All Employees** once.

With **Company** set on the server, only that company's employees are pushed.

## What is pushed

- Only employees with an **Attendance Device ID**. It is their code in BioTime.
- Their first and middle names as BioTime's first name, and their last name.
- Their department, area and position. One missing in BioTime is created, with the Frappe
  name as its code.

A push adds the employee's area and takes none away, because areas decide which terminals
know a person. Remove an area in BioTime when someone should stop using those terminals.

Saving an employee pushes them when one of these changes: Attendance Device ID, names,
Department, Branch, Designation, Status or Company. The push runs after the save, so a
save never waits for BioTime or fails because of it.

People in BioTime with no Frappe employee are never changed or deleted. Removing an
employee's Attendance Device ID does not remove them from BioTime.

## Employees who leave

When an employee's status becomes Left, **On Employee Left** decides what happens:

| On Employee Left | In BioTime |
|---|---|
| Resign in BioTime (default) | They are resigned from their Relieving Date. Their punches stay. |
| Do nothing | They stay as they are. |
| Delete in BioTime | They are deleted. |

BioTime 8.0 and 9.0 cannot resign people through their API. The server then says so,
and you resign them in BioTime. An employee who is Active again is reinstated.

Push All Employees applies this to employees who left before, too.

## A changed Attendance Device ID

The push finds the person in BioTime under the employee's old code and changes the code.
BioTime 9.5 does not change codes through its API: the person then keeps the old code, and
**Last Push Message** says so. Change the code in BioTime, or set the Attendance Device ID
back. No second person is created for them, because terminals would keep sending the old
code.

## How it went

**Last Push** and **Last Push Message** in the Status section show the last push. After
Push All Employees, the message counts what happened and lists up to 20 employees that
could not be pushed, with the reason. Unexpected failures are also in the Error Log.
