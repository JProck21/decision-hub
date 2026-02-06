-- Add quarantine_s3_key column to eval_audit_logs for storing rejected skill zips
ALTER TABLE eval_audit_logs ADD COLUMN IF NOT EXISTS quarantine_s3_key TEXT;
