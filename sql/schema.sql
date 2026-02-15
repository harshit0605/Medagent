CREATE TABLE IF NOT EXISTS patients (
  id TEXT PRIMARY KEY,
  phone TEXT UNIQUE NOT NULL,
  name TEXT,
  dob DATE,
  cohort_flags JSONB DEFAULT '{}'::jsonb,
  consent_flags JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS caregivers (
  id TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL REFERENCES patients(id),
  caregiver_phone TEXT NOT NULL,
  permissions JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS prescriptions (
  id TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL REFERENCES patients(id),
  uploaded_url TEXT NOT NULL,
  parsed_json JSONB DEFAULT '{}'::jsonb,
  verified_by_human BOOLEAN DEFAULT FALSE,
  expiry_date DATE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS regimens (
  id TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL REFERENCES patients(id),
  medication_name TEXT NOT NULL,
  salt TEXT,
  dose TEXT NOT NULL,
  schedule JSONB NOT NULL,
  start_at TIMESTAMPTZ,
  end_at TIMESTAMPTZ,
  strictness TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS adherence_events (
  id TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL REFERENCES patients(id),
  regimen_id TEXT NOT NULL REFERENCES regimens(id),
  scheduled_at TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL,
  confirmed_at TIMESTAMPTZ,
  channel_message_id TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS alerts (
  id TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL REFERENCES patients(id),
  type TEXT NOT NULL,
  severity TEXT NOT NULL,
  opened_at TIMESTAMPTZ DEFAULT NOW(),
  closed_at TIMESTAMPTZ,
  assigned_to TEXT
);

CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL REFERENCES patients(id),
  items JSONB NOT NULL,
  partner TEXT NOT NULL,
  status TEXT NOT NULL,
  receipt_url TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS templates (
  id TEXT PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  language TEXT NOT NULL,
  body TEXT NOT NULL,
  variables JSONB DEFAULT '[]'::jsonb,
  category TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_patients_phone ON patients(phone);
CREATE INDEX IF NOT EXISTS idx_adherence_events_patient_scheduled_at ON adherence_events(patient_id, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_alerts_patient_opened_closed ON alerts(patient_id, opened_at, closed_at);

CREATE TABLE IF NOT EXISTS lab_followups (
  id TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL REFERENCES patients(id),
  test_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'due',
  booked_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  reviewed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS appointment_followups (
  id TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL REFERENCES patients(id),
  clinician_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'due',
  booked_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  reviewed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ops_tickets (
  id TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL REFERENCES patients(id),
  category TEXT NOT NULL,
  priority TEXT NOT NULL,
  sla_minutes INT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  acknowledged_at TIMESTAMPTZ,
  resolved_at TIMESTAMPTZ,
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lab_followups_patient_status ON lab_followups(patient_id, status);
CREATE INDEX IF NOT EXISTS idx_appointment_followups_patient_status ON appointment_followups(patient_id, status);
CREATE INDEX IF NOT EXISTS idx_ops_tickets_status_priority ON ops_tickets(status, priority);
