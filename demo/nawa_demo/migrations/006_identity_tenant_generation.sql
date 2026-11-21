-- Synthetic-only extension: identity and tenant active-state changes also
-- invalidate process-local sessions when checked on the next request.
CREATE SEQUENCE nawa_identity_generation_seq AS bigint;
CREATE SEQUENCE nawa_tenant_generation_seq AS bigint;

ALTER TABLE nawa_identities ADD COLUMN identity_generation bigint NOT NULL
    DEFAULT nextval('nawa_identity_generation_seq');
ALTER TABLE nawa_tenants ADD COLUMN tenant_generation bigint NOT NULL
    DEFAULT nextval('nawa_tenant_generation_seq');

CREATE FUNCTION nawa_set_identity_generation() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.identity_generation := nextval('nawa_identity_generation_seq');
    ELSIF NEW.active IS DISTINCT FROM OLD.active THEN
        NEW.identity_generation := nextval('nawa_identity_generation_seq');
    ELSE
        NEW.identity_generation := OLD.identity_generation;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION nawa_set_tenant_generation() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.tenant_generation := nextval('nawa_tenant_generation_seq');
    ELSIF NEW.active IS DISTINCT FROM OLD.active THEN
        NEW.tenant_generation := nextval('nawa_tenant_generation_seq');
    ELSE
        NEW.tenant_generation := OLD.tenant_generation;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER nawa_identity_generation_trigger
BEFORE INSERT OR UPDATE ON nawa_identities
FOR EACH ROW EXECUTE FUNCTION nawa_set_identity_generation();
CREATE TRIGGER nawa_tenant_generation_trigger
BEFORE INSERT OR UPDATE ON nawa_tenants
FOR EACH ROW EXECUTE FUNCTION nawa_set_tenant_generation();
