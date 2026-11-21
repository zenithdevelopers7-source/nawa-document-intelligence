"""Draft authorization policy. Not authentication or an enforcement boundary.

Only server-provisioned principal records may be passed to this policy. This
module is not yet connected to any endpoint or database query.
"""
from dataclasses import dataclass

PERMISSIONS = {
    'viewer': frozenset({'list', 'view', 'compare', 'review_view'}),
    'analyst': frozenset({'list', 'view', 'compare', 'review_view', 'load', 'review_start', 'correct'}),
    'reviewer': frozenset({'list', 'view', 'compare', 'review_view', 'decide'}),
}


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant: str
    role: str
    active: bool = True
    membership_generation: int = 0
    identity_generation: int = 0
    tenant_generation: int = 0


def authorized(principal, action, *, tenant):
    """Deny by default; the caller must resolve identity and resource tenant."""
    if not isinstance(principal, Principal) or principal.active is not True:
        return False
    if not all(isinstance(x, str) and x.strip() for x in (principal.subject, principal.tenant, tenant)):
        return False
    if principal.tenant != tenant or not isinstance(action, str) or not isinstance(principal.role, str):
        return False
    return action in PERMISSIONS.get(principal.role, frozenset())

