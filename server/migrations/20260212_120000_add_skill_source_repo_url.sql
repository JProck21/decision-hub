-- Add source_repo_url to skills for linking to the original GitHub repository.
ALTER TABLE skills ADD COLUMN IF NOT EXISTS source_repo_url TEXT;
