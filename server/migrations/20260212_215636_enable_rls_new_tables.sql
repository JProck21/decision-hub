-- Enable RLS on tables created after migration 010_enable_rls_all_tables.sql.
-- Same rationale: block Supabase PostgREST (anon/authenticated) access while
-- the backend (table owner via SQLAlchemy) bypasses RLS automatically.

ALTER TABLE skill_trackers ENABLE ROW LEVEL SECURITY;
ALTER TABLE skill_access_grants ENABLE ROW LEVEL SECURITY;
