-- Initial PostgreSQL schema draft. Apply via migrations after review.
CREATE TABLE app_user (
  id BIGSERIAL PRIMARY KEY,
  telegram_id BIGINT UNIQUE NOT NULL,
  username TEXT,
  first_name TEXT,
  gender TEXT CHECK (gender IN ('male','female') OR gender IS NULL),
  height_cm SMALLINT CHECK (height_cm BETWEEN 80 AND 250),
  initial_weight_kg NUMERIC(5,2),
  current_weight_kg NUMERIC(5,2),
  goal TEXT,
  target_weight_kg NUMERIC(5,2),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE measurement (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  weight_kg NUMERIC(5,2),
  waist_cm NUMERIC(5,2),
  hips_cm NUMERIC(5,2),
  chest_cm NUMERIC(5,2),
  measured_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE food_entry (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  photo_url TEXT,
  meal_type TEXT NOT NULL DEFAULT 'other',
  detected_foods JSONB NOT NULL DEFAULT '[]'::jsonb,
  original_analysis JSONB,
  user_correction TEXT,
  corrected_analysis JSONB,
  balance_score SMALLINT CHECK (balance_score BETWEEN 1 AND 10),
  ai_comment TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE notification_settings (
  user_id BIGINT PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
  breakfast_enabled BOOLEAN NOT NULL DEFAULT true,
  breakfast_time TIME NOT NULL DEFAULT '08:30',
  lunch_enabled BOOLEAN NOT NULL DEFAULT true,
  lunch_time TIME NOT NULL DEFAULT '13:30',
  dinner_enabled BOOLEAN NOT NULL DEFAULT true,
  dinner_time TIME NOT NULL DEFAULT '19:30',
  quiet_hours_start TIME NOT NULL DEFAULT '22:00',
  quiet_hours_end TIME NOT NULL DEFAULT '08:00',
  weekly_report_enabled BOOLEAN NOT NULL DEFAULT true
);
