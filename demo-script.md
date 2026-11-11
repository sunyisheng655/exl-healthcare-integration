# Healthcare Integration Workbench demo

Target video length: **2 minutes 40 seconds**. Read the quoted lines aloud. Use the actions to guide the screen recording.

**0:00–0:28 · Start a generation job**

**On screen.** Open the [local workbench](http://127.0.0.1:8790/). In **New Integration Job**, click **Use example**, check **Allow automatic repair**, and click **Generate integration code**. Show the job starting under **Job Progress**.

> Our workbench connects three mock systems: a patient registration API, an appointments database, and a CSV file of observations. I ask the agent to combine the records by synthetic patient ID. I enable automatic repair so the agent can revise its code if a development test fails.

**0:28–0:58 · Show the generated code and batch results**

**On screen.** In **Job History**, select the completed **Model-generated job** `cc97cab8eb534e2c`. Point to **Development tests 11 / 11** and **Holdout tests 10 / 10**. Under **Attempt 1**, open **View source code** and briefly show the generated Python.

> Here’s a completed run of the same task. The agent checks how each source is structured and writes a Python adapter. An independent validator runs the adapter on a batch of test cases. This version passed all eleven development tests and all ten holdout tests.

**0:58–1:42 · Show the failure and repair**

**On screen.** In **Job History**, select **Controlled repair demo** `f7a4880965334a95`. In **Attempt 1**, point to **3 / 11** and open the `patient-P001` failure if it is collapsed. Show **Expected: 3** and **Actual: 1**. In **Attempt 2**, point to **11 / 11** and open **View integration plan** to show the change. Point to the **10 / 10** holdout result at the top of the report.

> For this repair demo, we deliberately started with a faulty adapter. For P zero zero one, the validator expected three appointments and got one. The model used that feedback to fix the code. The second version passed all eleven development tests, and then passed all ten holdout tests.

**1:42–2:07 · Approve the repaired version**

**On screen.** Stay on the **Controlled repair demo**. In **Version Approval**, point to `candidate-02.py` and its SHA-256 hash, then click **Approve this version**. Wait for the **Approved** status.

> I’ve reviewed the repaired candidate and its test report. I now approve this exact version. The code hash ties my approval to the file that passed the tests.

**2:07–2:40 · Look up P001**

**On screen.** In **Patient Lookup**, leave `P001` in **Synthetic patient ID** and click **Look up patient**. Scroll through the patient details, **Appointments (3)**, and **Observations (5)**. Show the REST, SQLite, and CSV source labels.

> I’ll look up P zero zero one. The approved Python code reads the three sources directly. Here are the patient’s registration details, three appointments, and five observations. Each section shows where its data came from.

For the recording, switch to the saved completed run as soon as the new job starts. The saved generation took about 139 seconds. To rehearse without starting a fresh model call, fill the example and go straight to **Job History**. Leave the repaired version unapproved until the take that shows the approval click. If you record again after approval, point to the **Approved** status and say, “I approved this exact version in the previous run,” then look up `P001`.

The two saved jobs are in the local workbench’s job history. A fresh clone starts with an empty history.
