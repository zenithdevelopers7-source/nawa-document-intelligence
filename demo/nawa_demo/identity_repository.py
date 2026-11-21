"""Draft provisioned-membership lookup; not connected to HTTP endpoints."""
import json
from .authorization import Principal


def membership(connection, *, issuer, subject, tenant, lock=False):
    if not all(isinstance(x, str) and x.strip() for x in (issuer, subject, tenant)):
        return None
    with connection.cursor() as cursor:
        query = '''SELECT m.role, m.membership_generation, i.identity_generation, t.tenant_generation FROM nawa_memberships m
            JOIN nawa_identities i ON i.issuer = m.issuer AND i.subject = m.subject
            JOIN nawa_tenants t ON t.id = m.tenant_id
            WHERE m.issuer = %s AND m.subject = %s AND m.tenant_id = %s
              AND m.active AND i.active AND t.active'''
        if lock:
            query += ' FOR SHARE OF m, i, t'
        cursor.execute(query, (issuer, subject, tenant))
        row = cursor.fetchone()
    if row is None:
        return None
    # The server-side session uses an unambiguous identity key. It must not be
    # accepted from the browser or logged as a credential.
    pair = json.dumps([issuer, subject], ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return Principal(pair.decode('utf-8'), tenant, row[0], membership_generation=row[1],
                     identity_generation=row[2], tenant_generation=row[3])


def membership_by_key(connection, *, identity_key, tenant, lock=False):
    """Recheck the exact issuer/subject membership for an existing session."""
    try:
        pair = json.loads(identity_key)
    except (TypeError, ValueError):
        return None
    if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(x, str) for x in pair):
        return None
    return membership(connection, issuer=pair[0], subject=pair[1], tenant=tenant, lock=lock)

