"""Indonesian patent expiry, annuity and Working Statement tracking.

The package is split into:

* ``idpatent.domain``        – pure, dependency-free legal/calculation logic
* ``idpatent.compliance``    – UU PDP / UU ITE send-gating, click-wrap, audit chain
* ``idpatent.notifications`` – localized templates with enforced compliance footer
* ``idpatent.security``      – passwords, TOTP MFA, field encryption, RBAC
* ``idpatent.ingestion``     – bulk file parsing and data-source adapters (PDKI etc.)
* ``idpatent.db`` / ``idpatent.services`` / ``idpatent.api`` – persistence and HTTP API
"""

__version__ = "0.1.0"
