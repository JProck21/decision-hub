-- Add skill visibility and cross-org access grants for private skills.
--
-- Skills default to 'public' (visible to everyone). Setting visibility
-- to 'org' restricts the skill to org members and explicitly granted
-- organisations.

-- 1. Add visibility column to skills table
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'skills' AND column_name = 'visibility'
    ) THEN
        ALTER TABLE skills ADD COLUMN visibility VARCHAR(10) NOT NULL DEFAULT 'public';
    END IF;
END
$$;

-- 2. Create skill_access_grants table for cross-org sharing
CREATE TABLE IF NOT EXISTS skill_access_grants (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    skill_id      UUID NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    grantee_org_id UUID NOT NULL REFERENCES organizations(id),
    granted_by    UUID NOT NULL REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (skill_id, grantee_org_id)
);
