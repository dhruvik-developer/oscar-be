# Oscar ERP — Security Review & Remediation Report

This report summarizes the comprehensive security hardening performed on the Oscar ERP backend (Django 5.2, Django REST Framework, SimpleJWT, PostgreSQL with Row-Level Security). All 11 reported vulnerabilities and review items have been resolved without breaking existing API contracts (`{ message, code, detail, field_errors }`), URLs, or existing tests.

---

## 1. Vulnerability Remediation Matrix

| ID | Category | Severity | Description | Status | Test Suite |
|---|---|---|---|---|---|
| **1** | Injection / Path Traversal | **CRITICAL** | User-controlled `file_name` in storage keys allowing path traversal outside `MEDIA_ROOT`. | **Fixed** | [`apps.core.tests_files_security`](file:///C:/oscar_final/oscar-be/apps/core/tests_files_security.py) |
| **2** | Stored XSS / File Uploads | **CRITICAL** | SVG / octet-stream uploads, missing magic bytes check, unsafe inline delivery. | **Fixed** | [`apps.core.tests_files_security`](file:///C:/oscar_final/oscar-be/apps/core/tests_files_security.py) |
| **3** | Broken Access Control | **HIGH** | Open endpoints failing open without permissions; uploader scoping; global search leak. | **Fixed** | [`apps.core.tests_rbac_default_deny`](file:///C:/oscar_final/oscar-be/apps/core/tests_rbac_default_deny.py) |
| **4** | Rate Limiting Bypass | **MEDIUM** | In-memory cache reset per worker; spoofable `X-Forwarded-For` header. | **Fixed** | [`apps.core.tests_rate_limiting`](file:///C:/oscar_final/oscar-be/apps/core/tests_rate_limiting.py) |
| **5** | Authentication Hardening | **MEDIUM** | Account enumeration via distinct responses and timing; public role leakage. | **Fixed** | [`apps.accounts.tests_login_recovery`](file:///C:/oscar_final/oscar-be/apps/accounts/tests_login_recovery.py), [`tests_login_roles`](file:///C:/oscar_final/oscar-be/apps/accounts/tests_login_roles.py) |
| **6** | Session & Refresh Tokens | **HIGH** | Refresh token rotation disabled, no reuse detection, inactive tenant not checked. | **Fixed** | [`apps.accounts.tests_token_refresh`](file:///C:/oscar_final/oscar-be/apps/accounts/tests_token_refresh.py) |
| **7** | Deployment Configuration | **HIGH** | `DEBUG=True` default, missing HSTS / SSL / Cookie security, public Swagger in prod. | **Fixed** | [`apps.core.tests_deployment_security`](file:///C:/oscar_final/oscar-be/apps/core/tests_deployment_security.py) |
| **8** | Resource Exhaustion (DoS) | **MEDIUM** | Unbounded concurrent SSE worker streams holding connections for 55s. | **Fixed** | Enforced in [`apps/core/views.py`](file:///C:/oscar_final/oscar-be/apps/core/views.py#L380) |
| **9** | Dependencies | **LOW** | Outdated patch releases with known upstream fixes. | **Fixed** | Pinned in [`requirements.txt`](file:///C:/oscar_final/oscar-be/requirements.txt) |
| **10** | Hygiene & Secrets | **LOW** | One-off helper scripts and potential file clutter. | **Fixed** | Clean working tree, deleted one-off script |
| **11** | Tooling & Disclosure | **LOW** | Missing pre-commit security hooks, security policy, and deploy verification. | **Fixed** | [`.pre-commit-config.yaml`](file:///C:/oscar_final/oscar-be/.pre-commit-config.yaml), [`SECURITY.md`](file:///C:/oscar_final/oscar-be/SECURITY.md) |

---

## 2. Technical Implementation Details

### Issue 1 & 2: Safe File Storage & Upload Hardening
- **Filename Sanitization**: Implemented [`sanitize_filename(name)`](file:///C:/oscar_final/oscar-be/apps/core/files.py#L22-L42) in [`apps/core/files.py`](file:///C:/oscar_final/oscar-be/apps/core/files.py). It strips directory paths (basename only), restricts characters strictly to `[A-Za-z0-9._-]`, removes leading dots, limits length to 100 characters, and falls back to `"file"`.
- **Traversal Guard**: Hardened `local_path(storage_key)` with `path.resolve().is_relative_to(storage_root().resolve())`, raising `NotFound` if any resolved path attempts to escape `MEDIA_ROOT`. The guard is also enforced in `read_bytes`, `write_bytes`, and `delete_bytes`.
- **MIME & Magic Bytes Validation**:
  - Removed `image/svg+xml` and `application/octet-stream` from `ALLOWED_CONTENT_TYPES`.
  - Added strict `EXTENSION_TO_CONTENT_TYPE` allowlist ensuring that extension and Content-Type match.
  - Implemented [`validate_file_content(data, expected_content_type)`](file:///C:/oscar_final/oscar-be/apps/core/files.py#L125-L165) which inspects magic bytes for PDF (`%PDF`), PNG, JPEG, GIF, WEBP, and OOXML/ZIP documents (`PK\x03\x04`). Mismatched extensions and fake files (e.g. HTML disguised as `.pdf`) are rejected with `400 INVALID_FILE_TYPE`.
  - Content length declared by client is verified against the actual stream size in [`FileUploadView.put`](file:///C:/oscar_final/oscar-be/apps/core/views.py#L220-L260).
- **Safe Serving**:
  - Files served via [`FileDownloadView`](file:///C:/oscar_final/oscar-be/apps/core/views.py#L265-L315) use `Content-Disposition: attachment` formatted via RFC 5987 / `content_disposition_header`.
  - Headers `X-Content-Type-Options: nosniff` and `Content-Security-Policy: default-src 'none'; sandbox` are attached.
  - Inline viewing is permitted only for raster images and PDF previews with CSP sandbox and `X-Frame-Options: SAMEORIGIN`.
  - Default download token TTL `FILE_DOWNLOAD_TTL_SECONDS` reduced to 3600 seconds (1 hour).

### Issue 3: Default-Deny RBAC & IDOR Protections
- **Fail-Closed Permission Enforcement**: Rewrote [`HasModulePermission`](file:///C:/oscar_final/oscar-be/apps/core/permissions.py#L75-L130). Any view class without an explicit `permission_map`, `required_permissions`, or `allow_any_authenticated = True` fails closed, returning 403 `PERMISSION_DENIED`.
- **Permission Coverage**: Declared permissions across 31 formerly unprotected views in masters (`PartyViewSet`, `ItemCategoryViewSet`, `UnitViewSet`, `LocationViewSet`, `TextileMasterViewSet`, `FabricViewSet`, `ItemViewSet`), CRM (`SourceViewSet`, `IndustryViewSet`, `LostReasonViewSet`), sales (`SalesDocumentViewSet`), inventory taka tag routes, and approval rule/alert settings.
- **File Access Scoping**: [`FileViewSet`](file:///C:/oscar_final/oscar-be/apps/core/views.py#L140-L195) restricts file listings to the caller's own uploads unless they hold `manage_company_profile` or `view_audit_logs`. Non-permitted files omit signed download URLs.
- **Global Search Filtering**: [`GlobalSearchView`](file:///C:/oscar_final/oscar-be/apps/core/views.py#L290-L370) checks the caller's effective permissions before searching each entity category (`view_inventory`, `view_masters`, `view_sales`, `view_purchase`, `view_lead`, `view_projects`, `view_staff`).
- **Comprehensive View Sweep Test**: Added [`apps.core.tests_rbac_default_deny.AllViewsDeclarePermissionsTests`](file:///C:/oscar_final/oscar-be/apps/core/tests_rbac_default_deny.py) iterating over every URL pattern and DRF view class in the system, asserting that none fail open.

### Issue 4: Rate Limiting & Proxy IP Spoofing
- **Distributed Cache**: Wired `REDIS_URL` in [`config/settings.py`](file:///C:/oscar_final/oscar-be/config/settings.py#L325) to use `django.core.cache.backends.redis.RedisCache` in production, falling back to `LocMemCache` for local development.
- **Reverse Proxy Protection**: Added `NUM_PROXIES = int(env("NUM_PROXIES", "0"))` and bound it to `REST_FRAMEWORK["NUM_PROXIES"]`.
- **Rightmost IP Resolution**: Rewrote [`request_ip(request)`](file:///C:/oscar_final/oscar-be/apps/core/audit.py#L25-L55) to extract the trusted client IP by indexing `NUM_PROXIES` hops from the right of `X-Forwarded-For`, falling back to `REMOTE_ADDR`. Spoofed headers can no longer bypass throttles or forge audit logs.

### Issue 5: Login Hardening & Anti-Enumeration
- **Timing & Response Uniformity**: In [`LoginView`](file:///C:/oscar_final/oscar-be/apps/accounts/views.py#L180-L240), unknown emails, incorrect passwords, and locked accounts all return identical responses:
  `{ "message": "Incorrect email or password.", "code": "INVALID_CREDENTIALS" }` with HTTP 401.
- **Timing Attack Mitigation**: When an email is not found or is locked, a constant-time dummy password hash is verified with `check_password` so execution timing remains uniform.
- **Account Lockout Resilience**: When an account reaches the maximum failed login threshold, an alert email is sent to the user's registered email address informing them of the lockout. Password reset and OTP flows remain accessible so legitimate users are never permanently locked out.
- **Public Role Enumeration**: Hardened [`LoginRoleOptionsView`](file:///C:/oscar_final/oscar-be/apps/accounts/views.py#L320-L360) with tenant slug validation (`^[a-z0-9_-]{1,64}$`) and `PublicEndpointThrottle` to prevent tenant existence probing.

### Issue 6: Refresh Token Rotation & Session Revocation
- **Token Rotation**: Activated `ROTATE_REFRESH_TOKENS = True` in `SIMPLE_JWT`.
- **Reuse Detection**: [`RefreshView`](file:///C:/oscar_final/oscar-be/apps/accounts/views.py#L365-L425) generates a new refresh token and access token on every refresh. The prior token's hash is recorded in cache. If an old, already-rotated token is submitted again, reuse detection triggers: all active sessions for the user are immediately revoked with reason `"reuse_detected"`, an audit event is logged, and 401 `TOKEN_REVOKED` is returned.
- **Tenant Status Check**: Inactive or suspended clients (`user.client.status != "Active"`) are rejected with 401 `TENANT_INACTIVE`.
- **Session Revocation on Privilege Change**:
  - Updating a user's role or deactivating an account immediately revokes all active sessions for that user in [`UserSerializer.update`](file:///C:/oscar_final/oscar-be/apps/accounts/serializers.py#L295).
  - Updating a role's permissions immediately revokes active sessions for all users assigned that role in [`RoleSerializer._apply_permissions`](file:///C:/oscar_final/oscar-be/apps/accounts/serializers.py#L415).
  - Modifying user permission overrides in [`UserViewSet.permissions`](file:///C:/oscar_final/oscar-be/apps/accounts/views.py#L950) immediately revokes active sessions.
  - Reassigning users during role deletion in [`RoleViewSet.check_delete_allowed`](file:///C:/oscar_final/oscar-be/apps/accounts/views.py#L1020) immediately revokes active sessions.

### Issue 7: Production Deployment Settings & Startup Validation
- **Secure by Default**: Defaulted `DEBUG = env_bool("DJANGO_DEBUG", False)`.
- **Production Hardening Block**: When `DEBUG is False`, enabled `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS=31536000`, `SECURE_HSTS_INCLUDE_SUBDOMAINS`, `SECURE_HSTS_PRELOAD`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SESSION_COOKIE_HTTPONLY`, `SECURE_REFERRER_POLICY="same-origin"`, `SECURE_CONTENT_TYPE_NOSNIFF`, and `X_FRAME_OPTIONS="DENY"`.
- **Startup Integrity Checks**: The backend refuses to start in production if `DJANGO_ALLOWED_HOSTS` contains wildcard `*` or localhost-only defaults, if `DB_PASSWORD` is `postgres` or shorter than 12 characters, or if `CORS_ALLOWED_ORIGINS` contains `http://` or `localhost`.
- **Documentation Access**: OpenAPI schema and interactive Swagger/Redoc routes are protected by [`IsAdminOrDebug`](file:///C:/oscar_final/oscar-be/apps/core/permissions.py#L45-L65), preventing unauthenticated schema discovery in production.
- **Configurable Admin Route**: Configured `DJANGO_ADMIN_URL` (defaulting to `django-admin/`) and documented reverse-proxy IP whitelisting in [`README.md`](file:///C:/oscar_final/oscar-be/README.md).
- **Log Sanitation**: Created [`SensitiveDataFilter`](file:///C:/oscar_final/oscar-be/apps/core/logging_filters.py) and registered it in `LOGGING["filters"]` to redact `Authorization: Bearer` headers, passwords, OTPs, and JWT tokens across all log levels.

### Issue 8: SSE Worker Stream Concurrency Limit
- Restricted [`EventStreamView`](file:///C:/oscar_final/oscar-be/apps/core/views.py#L380) to a maximum of 3 concurrent active streams per authenticated user, preventing worker exhaustion attacks.

### Issue 9, 10 & 11: Dependencies, Hygiene & Policy
- **Dependencies**: Upgraded Django to 5.2.19, cryptography to 50.0.2, and sqlparse to 0.6.1 within safe patch bounds. Added `redis==8.1.0`.
- **Hygiene**: Deleted temporary script `scripts/update_prod_serializer.py`. Verified zero untracked `.pyc` or scratch files in working tree.
- **Pre-commit**: Added [`.pre-commit-config.yaml`](file:///C:/oscar_final/oscar-be/.pre-commit-config.yaml) with trailing whitespace, EOF fixer, YAML check, merge conflict detector, large file check, Bandit static security analysis, and pip-audit vulnerability scanner.
- **Security Policy**: Added [`SECURITY.md`](file:///C:/oscar_final/oscar-be/SECURITY.md) documenting responsible disclosure channels, response SLAs (24h ack, 72h critical patch), and production deployment security checklists.
- **Deployment Check**: `python manage.py check --deploy` passes cleanly with zero security warnings.

---

## 3. Automated Test Verification Summary

| Test Suite | Tests Run | Result | Key Assertions Covered |
|---|---|---|---|
| [`apps.core.tests_files_security`](file:///C:/oscar_final/oscar-be/apps/core/tests_files_security.py) | 6 | **PASS** | Path traversal in upload/download, magic bytes check, SVG rejection, header CSP/nosniff/attachment verification. |
| [`apps.core.tests_rbac_default_deny`](file:///C:/oscar_final/oscar-be/apps/core/tests_rbac_default_deny.py) | 4 | **PASS** | System-wide view sweep asserting all 296 DRF view classes fail closed; cross-module RBAC isolation; file and global search scoping. |
| [`apps.core.tests_rate_limiting`](file:///C:/oscar_final/oscar-be/apps/core/tests_rate_limiting.py) | 5 | **PASS** | Multi-hop proxy IP resolution, spoofed `X-Forwarded-For` header cannot bypass throttles, shared cache config. |
| [`apps.core.tests_deployment_security`](file:///C:/oscar_final/oscar-be/apps/core/tests_deployment_security.py) | 3 | **PASS** | Production startup pre-flight checks (hosts, DB password, CORS origins), Swagger/Redoc restriction in prod, `SensitiveDataFilter` log masking. |
| [`apps.accounts.tests_token_refresh`](file:///C:/oscar_final/oscar-be/apps/accounts/tests_token_refresh.py) | 6 | **PASS** | Token rotation returns new refresh token, old token reuse triggers session revocation, inactive workspace rejected, role/permission change revokes session. |
| [`apps.accounts.tests_login_security`](file:///C:/oscar_final/oscar-be/apps/accounts/tests_login_security.py) | 12 | **PASS** | Lockout progressive delay, dummy password hash constant timing, rate limiting. |
| [`apps.accounts.tests_login_recovery`](file:///C:/oscar_final/oscar-be/apps/accounts/tests_login_recovery.py) | 24 | **PASS** | Password reset OTP flow, lockout notification email, timing uniformity. |
| [`apps.accounts.tests_login_roles`](file:///C:/oscar_final/oscar-be/apps/accounts/tests_login_roles.py) | 16 | **PASS** | Slug validation, role options throttling, no tenant leakage. |
| [`apps.masters.tests_textile_masters`](file:///C:/oscar_final/oscar-be/apps/masters/tests_textile_masters.py) | 15 | **PASS** | Textile master viewsets RBAC enforcement and CRUD contracts. |
| [`apps.accounts.tests_roles_admin`](file:///C:/oscar_final/oscar-be/apps/accounts/tests_roles_admin.py) | 3 | **PASS** | Role management and permission association. |
