-- Synthetic QA only. Each grant change gets a new database generation.
CREATE SEQUENCE nawa_synthetic_grant_generation_seq AS bigint;
ALTER TABLE nawa_synthetic_document_grants ADD COLUMN grant_generation bigint NOT NULL
    DEFAULT nextval('nawa_synthetic_grant_generation_seq');

CREATE FUNCTION nawa_synthetic_set_grant_generation() RETURNS trigger AS $$
BEGIN
    IF (NEW.membership_generation, NEW.identity_generation, NEW.tenant_generation, NEW.active)
       IS DISTINCT FROM (OLD.membership_generation, OLD.identity_generation, OLD.tenant_generation, OLD.active) THEN
        NEW.grant_generation := nextval('nawa_synthetic_grant_generation_seq');
    ELSE
        NEW.grant_generation := OLD.grant_generation;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER nawa_synthetic_grant_generation_trigger
BEFORE UPDATE ON nawa_synthetic_document_grants
FOR EACH ROW EXECUTE FUNCTION nawa_synthetic_set_grant_generation();
