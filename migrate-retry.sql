-- Add retry columns to pending_emails table
ALTER TABLE pending_emails ADD COLUMN IF NOT EXISTS retry_count INTEGER DEFAULT 0;
ALTER TABLE pending_emails ADD COLUMN IF NOT EXISTS next_retry TIMESTAMPTZ DEFAULT now();
