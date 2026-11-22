"""Role-based access control with least privilege (SRS 10.2).

Separation of duties: account managers draft and send communications but
cannot approve bulk/cold campaigns or attest counsel review of templates.
"""

from __future__ import annotations

from enum import Enum

from ..domain.enums import Role


class Permission(str, Enum):
    PATENT_READ = "patent:read"
    PATENT_WRITE = "patent:write"
    PATENT_IMPORT = "patent:import"
    PATENT_VERIFY = "patent:verify"  # record verification / manual override
    OWNER_READ = "owner:read"
    OWNER_WRITE = "owner:write"
    CONTACT_READ = "contact:read"
    CONTACT_READ_PII = "contact:read_pii"  # unmasked e-mail / phone
    CONTACT_WRITE = "contact:write"
    ALERT_READ = "alert:read"
    ALERT_MANAGE = "alert:manage"
    COMM_DRAFT = "comm:draft"
    COMM_APPROVE = "comm:approve"
    COMM_SEND = "comm:send"
    COMPLIANCE_MANAGE = "compliance:manage"  # suppression list, LIAs, DSAR exports
    TEMPLATE_APPROVE = "template:approve"  # attest Indonesian counsel review
    AGREEMENT_MANAGE = "agreement:manage"
    USER_MANAGE = "user:manage"
    AUDIT_READ = "audit:read"
    REPORT_READ = "report:read"
    SETTINGS_MANAGE = "settings:manage"
    PORTAL_SELF = "portal:self"  # client portal: own portfolio only
    PLATFORM_ADMIN = "platform:admin"  # cross-firm administration


_ACCOUNT_MANAGER = frozenset(
    {
        Permission.PATENT_READ, Permission.PATENT_WRITE, Permission.PATENT_IMPORT, Permission.PATENT_VERIFY,
        Permission.OWNER_READ, Permission.OWNER_WRITE, Permission.CONTACT_READ, Permission.CONTACT_READ_PII,
        Permission.CONTACT_WRITE, Permission.ALERT_READ, Permission.ALERT_MANAGE, Permission.COMM_DRAFT,
        Permission.COMM_SEND, Permission.REPORT_READ,
    }
)
_VIEWER = frozenset(
    {Permission.PATENT_READ, Permission.OWNER_READ, Permission.CONTACT_READ, Permission.ALERT_READ, Permission.REPORT_READ}
)
_FIRM_ADMIN = frozenset(set(Permission) - {Permission.PLATFORM_ADMIN, Permission.PORTAL_SELF})

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.SUPER_ADMIN: frozenset(set(Permission) - {Permission.PORTAL_SELF}),
    Role.FIRM_ADMIN: _FIRM_ADMIN,
    Role.ACCOUNT_MANAGER: _ACCOUNT_MANAGER,
    Role.VIEWER: _VIEWER,
    Role.CLIENT_USER: frozenset({Permission.PORTAL_SELF}),
}


def has_permission(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())
