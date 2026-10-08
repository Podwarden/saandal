# Security Policy

saandal processes model output that can contain secrets (it ships an outgoing
secret-scrubber) and interacts with a running hermes-agent. Please report
vulnerabilities responsibly.

## Reporting a vulnerability
- **Preferred:** open a private [GitHub Security Advisory](../../security/advisories/new)
  ("Report a vulnerability").
- **Do not** open a public issue for a security problem.
- Include reproduction steps and the affected version/commit.

We aim to acknowledge reports within a few business days.

## Supported versions
The latest revision on the default branch is supported. Older revisions are not
maintained.

## In scope
saandal's guards are fail-safe by design. Reports showing a guard can be made to
leak a secret, crash a turn, bypass the secret-scrubber, or cause a hermes turn
to hang are in scope.
