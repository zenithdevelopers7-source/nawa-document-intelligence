"""Container-only entrypoint for the Phase 9 synthetic dashboard.

The existing dashboard CLI stays bound to 127.0.0.1 for Mac/local use.
This container process binds 0.0.0.0 *inside its isolated container* so
Docker can publish 127.0.0.1:8765 on the host. Never publish to a LAN IP.
"""
from http.server import HTTPServer
import os
from urllib.parse import urlparse
from .dashboard import DashboardState, make_handler


def serve():
    mode = os.environ.get('NAWA_STORAGE', 'memory')
    auth_mode = os.environ.get('NAWA_AUTH', 'demo')
    intake_mode = os.environ.get('NAWA_INTAKE', 'off')
    evidence_mode = os.environ.get('NAWA_SYNTHETIC_EVIDENCE', 'off')
    bind_port = int(os.environ.get('NAWA_BIND_PORT', '8765'))
    if not 1024 <= bind_port <= 65535:
        raise ValueError('NAWA_BIND_PORT must be an unprivileged TCP port')
    if auth_mode not in ('demo', 'oidc'):
        raise ValueError('NAWA_AUTH must be demo or oidc')
    if intake_mode not in ('off', 'synthetic'):
        raise ValueError('NAWA_INTAKE must be off or synthetic')
    if intake_mode == 'synthetic' and (auth_mode != 'oidc' or mode != 'postgres'):
        raise ValueError('Synthetic intake requires OIDC and PostgreSQL')
    if evidence_mode not in ('off', 'ci'):
        raise ValueError('Invalid synthetic evidence mode')
    if evidence_mode == 'ci' and (os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('POSTGRES_DB') != 'nawa_phase13_qa_demo'
            or intake_mode != 'synthetic' or auth_mode != 'oidc' or mode != 'postgres'):
        raise ValueError('Synthetic evidence requires the disposable CI stack')
    from .scoped_state import validate_correction_source_policy
    correction_policy = validate_correction_source_policy(
        os.environ.get('NAWA_SYNTHETIC_CORRECTION_SOURCE', 'legacy_asserted'))
    if correction_policy != 'legacy_asserted' and intake_mode != 'synthetic':
        raise ValueError('Verified synthetic corrections require synthetic intake')
    origin_policy = None
    if intake_mode == 'synthetic':
        from .intake_scoped import validate_origin_policy
        origin_policy = validate_origin_policy(os.environ.get('NAWA_SYNTHETIC_ORIGIN_POLICY', 'legacy_compatible'))
    if mode == 'memory':
        state = DashboardState()
    elif mode == 'postgres':
        import psycopg
        from .postgres_state import PostgresState
        def connect():
            return psycopg.connect(host=os.environ['POSTGRES_HOST'],
                                   dbname=os.environ['POSTGRES_DB'],
                                   user=os.environ['POSTGRES_USER'],
                                   password=os.environ['POSTGRES_PASSWORD'],
                                   connect_timeout=5)
        state = PostgresState(connect)
        state.storage_mode = 'postgres'
        from .storage_mode import claim_database_mode
        claim_database_mode(connect, auth_mode)
    else:
        raise ValueError('NAWA_STORAGE must be memory or postgres')
    if auth_mode == 'demo':
        handler = make_handler(state)
    elif auth_mode == 'oidc':
        if mode != 'postgres':
            raise ValueError('OIDC mode requires PostgreSQL storage')
        from .identity_repository import membership, membership_by_key
        from .oidc_attempts import LoginAttempts
        from .oidc_flow_draft import OidcFlow
        from .oidc_provider_draft import OidcProvider
        from .protected_api import make_protected_handler
        from .scoped_state import ScopedState
        from .session_draft import SessionStore
        issuer = os.environ['OIDC_ISSUER']
        client = os.environ['OIDC_CLIENT_ID']
        tenant = os.environ['NAWA_TENANT_ID']
        origin = os.environ.get('NAWA_WEB_ORIGIN', 'http://127.0.0.1:8765')
        parsed_origin = urlparse(origin)
        if (parsed_origin.scheme != 'http' or parsed_origin.hostname != '127.0.0.1'
                or not parsed_origin.port or parsed_origin.username or parsed_origin.password
                or parsed_origin.path or parsed_origin.params or parsed_origin.query or parsed_origin.fragment):
            raise ValueError('OIDC test mode requires an explicit loopback HTTP origin')
        if not tenant.strip():
            raise ValueError('OIDC mode requires a configured tenant')
        def lookup(identity_key, tenant_id):
            with connect() as conn:
                return membership_by_key(conn, identity_key=identity_key, tenant=tenant_id)
        def resolve(issuer_id, subject):
            with connect() as conn:
                return membership(conn, issuer=issuer_id, subject=subject, tenant=tenant)
        sessions = SessionStore(lookup)
        provider = OidcProvider(issuer, ca_file=os.environ.get('OIDC_CA_FILE') or None)
        flow = OidcFlow(issuer=issuer, client_id=client,
                        redirect_uri=origin + '/auth/callback',
                        provider=provider, attempts=LoginAttempts(), sessions=sessions,
                        resolve_membership=resolve)
        intake = None
        if intake_mode == 'synthetic':
            from .intake_runtime import build_synthetic_intake
            intake = build_synthetic_intake(connect, os.environ.get('NAWA_INTAKE_DIR'), origin_policy=origin_policy)
        scoped = ScopedState(connect, correction_source_policy=correction_policy,
                             correction_source_store=intake.store if intake is not None else None)
        retrieval = None
        review_queue = None
        if evidence_mode == 'ci':
            from .synthetic_live_retrieval import SyntheticLiveRetrieval
            from .synthetic_postgres_passages import SyntheticPostgresPassages
            from .synthetic_postgres_review_queue import SyntheticPostgresReviewQueue
            evidence = SyntheticPostgresPassages(connect, intake.store)
            retrieval = SyntheticLiveRetrieval(evidence.principal, evidence.passages)
            review_queue = SyntheticPostgresReviewQueue(retrieval, connect)
        handler = make_protected_handler(scoped, sessions, state, flow=flow,
                                         allowed_origin=origin, intake=intake, synthetic_retrieval=retrieval,
                                         synthetic_review_queue=review_queue)
    else:
        raise ValueError('NAWA_AUTH must be demo or oidc')
    with HTTPServer(('0.0.0.0', bind_port), handler) as server:
        print(f'NAWA synthetic Docker dashboard container port: {bind_port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    serve()
