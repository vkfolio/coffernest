# Security

Please report vulnerabilities privately through GitHub's "Report a vulnerability" button on this
repository (Security → Advisories), not in public issues. You'll get a reply within a week.

What's in place: Argon2id passwords, optional TOTP, short-lived access tokens with rotating
refresh tokens, sign-in lockouts, account numbers and PDF passwords encrypted at rest
(`SECRET_ENC_KEY`), and an audit log for sensitive reads.
