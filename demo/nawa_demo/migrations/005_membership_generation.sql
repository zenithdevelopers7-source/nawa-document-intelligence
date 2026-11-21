-- Synthetic identity experiment only. A changed membership gets a fresh
-- generation so deactivate/reactivate or role-change cycles invalidate old
-- process-local sessions on their next protected request.
CREATE SEQUENCE nawa_membership_generation_seq AS bigint;

ALTER TABLE nawa_memberships ADD COLUMN membership_generation bigint NOT NULL
    DEFAULT nextval('nawa_membership_generation_seq');

CREATE FUNCTION nawa_set_membership_generation() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.membership_generation := nextval('nawa_membership_generation_seq');
    ELSIF NEW.active IS DISTINCT FROM OLD.active OR NEW.role IS DISTINCT FROM OLD.role THEN
        NEW.membership_generation := nextval('nawa_membership_generation_seq');
    ELSE
        NEW.membership_generation := OLD.membership_generation;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER nawa_membership_generation_trigger
BEFORE INSERT OR UPDATE ON nawa_memberships
FOR EACH ROW EXECUTE FUNCTION nawa_set_membership_generation();
