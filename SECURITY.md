<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Security Policy

## Supported versions

| Version | Supported          |
| ------- | ------------------ |
| 0.7.x   | :white_check_mark: |
| < 0.7   | :x:                |

The `0.7.x` series is the first public release of
the `kntgraph` package (the successor of the
internal `fmh_backend` and `fmh_agents`
packages). Older versions (the `fmh_*` series)
are not supported and will not receive security
fixes — please upgrade.

## Reporting a vulnerability

**Do not open a public GitHub issue for security
vulnerabilities.**

Report security issues privately via one of these
channels:

1. **GitHub Security Advisories** (preferred):
   <https://github.com/kinetgraph/kntgraph/security/advisories/new>
2. **Email**:
   [security@kinetgraph.example](mailto:security@kinetgraph.example)
   (replace with the public security contact once
   the project is public).

A report should include:

- A description of the vulnerability and its
  impact.
- A minimal reproduction (script, snippet, or
  PoC).
- The version of `kntgraph` affected (output of
  `python -c "import kntgraph; print(kntgraph.__version__)"`).
- The Python version and OS.

You should receive an acknowledgement within 48
hours. We will follow up with a timeline for the
fix and a coordinated disclosure plan.

## Threat model

`kntgraph` is an agent framework that:

- Reads events from Redis Streams and folds them
  into a deterministic World.
- Calls external tools (LLM providers, HTTP
  APIs, internal services) on behalf of agents.
- Persists checkpoints and solution candidates
  in Redis / FalkorDB.
- Optionally signs events with Ed25519
  (`FMH_CRYPTO_ENABLED=1`).

Out of scope:

- Vulnerabilities in the LLM providers we
  integrate with (OpenAI, Anthropic, Ollama,
  …). Report those to the provider.
- Vulnerabilities in `redis.asyncio` or
  `pydantic`. Report those upstream.
- Vulnerabilities in `fakeredis` or
  `falkordblite` (dev-only extras). Report
  upstream.

In scope:

- Authentication / authorisation bypasses
  (`api/_auth/`, `security/`).
- Event signing bypasses
  (`security/signing/`).
- Idempotency-key collisions or replay
  vulnerabilities in `stream/event_log/`.
- DLQ data leakage or replay (`events/dlq/`).
- Injection in Cypher queries (`knowledge/falkordb/`).
- PII redaction bypasses (`agents/tools/pii/`).

## Cryptographic choices

- **Event signing**: Ed25519 over RFC 8785 JCS
  (JSON Canonicalisation Scheme). The
  `security/signing/` module is the only path
  that signs events; the `Event.signature`
  field is opt-in (a missing signature is treated
  as "unsigned" and is accepted for backwards
  compatibility, but operators are expected to
  enable signing in production).
- **API key auth**: SHA-256 of the raw key,
  stored as the first 16 hex chars. The full
  digest is **not** truncated; the storage is
  not security-sensitive (the digest is one-way,
  not a credential).
- **PII hashing**: SHA-256 of the value, truncated
  to 64 bits (`infra.hashing.short_hash`). Used
  as a stable identifier for "this person was
  seen" events; **not** a security primitive.

Report any cryptographic concerns to the
security contact above; we will coordinate with
the cryptography maintainers before disclosure.

## Coordinated disclosure timeline

| Day | Action                                                 |
| --- | ------------------------------------------------------ |
| 0   | Vulnerability reported privately.                      |
| 1-2 | Maintainers acknowledge receipt.                       |
| 7   | Triage: scope, impact, severity.                       |
| 14  | Patch developed in a private fork.                     |
| 30  | Patch released; CVE assigned (if applicable).         |
| 45  | Public advisory published; embargo lifts.             |

We may accelerate the timeline for actively
exploited vulnerabilities, and slow it down for
complex patches that need a longer review window.

## Recognition

We maintain a [security acknowledgements](SECURITY_ACKS.md)
list of reporters who have helped us improve
the security of `kntgraph`. Reporters who wish
to remain anonymous are not listed.
