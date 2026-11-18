# Security Policy

Evenmore ERP / Oscar ERP takes the security of our application, multi-tenant isolation, and customer data seriously.

---

## Supported Versions

Only the latest release line receives active security updates and vulnerability patches.

| Version | Supported          | Python  | Django     |
| ------- | ------------------ | ------- | ---------- |
| 1.x     | :white_check_mark: | 3.11+   | 5.2.x LTS  |
| < 1.0   | :x:                | All     | Unsupported|

---

## Reporting a Vulnerability (Responsible Disclosure)

**Please do NOT report security vulnerabilities via public GitHub issues, discussions, or pull requests.**

If you discover a potential security vulnerability in Oscar ERP:

1. **Email:** Send details to `security@swevenfabricators.com` (or the configured `SUPPORT_EMAIL`).
2. **Subject line:** `[SECURITY] Potential vulnerability in Oscar ERP`
3. **Details to include:**
   - Detailed description of the vulnerability and attack vector.
   - Proof of concept (PoC) code or exact reproduction steps (HTTP request/response).
   - Component / module affected (e.g., Auth, RBAC, File Storage, Accounting).
   - Estimated impact and severity assessment (e.g. CVSS score).
   - Proposed remediation, if known.

We ask that you give our security team reasonable time to investigate and address the vulnerability before publishing details or coordinating public disclosure.

---

## Response SLA

Our security response commitment:

- **Initial Acknowledgement:** Within **24 hours** of submission.
- **Triage & Severity Confirmation:** Within **48 hours** with an assigned severity rating.
- **Remediation Timelines:**
  - **Critical (CVSS 9.0 - 10.0):** Patch released within **72 hours**.
  - **High (CVSS 7.0 - 8.9):** Patch released within **7 days**.
  - **Medium (CVSS 4.0 - 6.9):** Patch released within **14 days**.
  - **Low (CVSS 0.1 - 3.9):** Addressed in the next planned maintenance sprint.

---

## Production Security Deployment Checklist

When deploying Oscar ERP to a production environment:

1. **Environment Settings:**
   - Ensure `DJANGO_DEBUG=False`.
   - Set a cryptographically secure `DJANGO_SECRET_KEY` (minimum 32 characters, generated with `secrets.token_urlsafe(50)`).
   - Configure `DJANGO_ALLOWED_HOSTS` with explicit fully qualified domain names (wildcard `*` and localhost defaults are blocked at startup).
   - Configure `CORS_ALLOWED_ORIGINS` with explicit HTTPS origins (HTTP and localhost origins are blocked at startup).
   - Provide a strong database password for `DB_PASSWORD` (minimum 12 characters, non-default).

2. **Network & Reverse Proxy:**
   - Configure `NUM_PROXIES` in `.env` matching your exact proxy topology (e.g., `1` for Nginx, `2` for Cloudflare + Nginx) to enforce secure IP parsing and prevent rate-limiting bypass.
   - Set `DJANGO_ADMIN_URL` to an unpredictable URL path, and restrict access to authorized corporate VPN/static IPs at the web server/reverse proxy level.

3. **Storage & Media:**
   - Store user uploads in dedicated object storage (S3/GCS) or an isolated filesystem path outside web root.
   - Files are served with `Content-Disposition: attachment` (or restricted sandboxed inline preview for raster images/PDFs), `X-Content-Type-Options: nosniff`, and `Content-Security-Policy: default-src 'none'; sandbox`.

4. **Multi-Tenancy & Access Control:**
   - Verify PostgreSQL Row-Level Security (RLS) is active across all tenant tables (`python manage.py enable_rls`).
   - All API endpoints adhere to default-deny RBAC permissions (`HasModulePermission`).
