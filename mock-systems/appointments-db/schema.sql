-- Synthetic appointments source; no real healthcare records.
-- person_no references the external registration API's patient_id by contract.
-- It is intentionally not a foreign key to a table in this SQLite database.
PRAGMA user_version = 1;
CREATE TABLE appointments (
    appt_id TEXT PRIMARY KEY NOT NULL,
    person_no TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    department_code TEXT NOT NULL CHECK (department_code IN ('GP', 'RAD', 'LAB')),
    visit_type INTEGER NOT NULL CHECK (visit_type IN (1, 2)),
    status_code TEXT NOT NULL CHECK (status_code IN ('B', 'C', 'D')),
    updated_at TEXT NOT NULL,
    CHECK (appt_id GLOB 'AP[0-9][0-9][0-9]'),
    CHECK (person_no GLOB 'P[0-9][0-9][0-9]'),
    CHECK (scheduled_at GLOB '????-??-??T??:??:??Z'),
    CHECK (updated_at GLOB '????-??-??T??:??:??Z')
);
CREATE INDEX idx_appointments_person_no ON appointments(person_no);
