# Licensing

HealthClaw Guardrails is **open source under the [MIT license](LICENSE)** —
use it, modify it, build on it, commercially or otherwise.

## A note on the future

As adoption grows, **future releases may move to a Fair Source license**
(e.g. FSL-1.1-MIT — free for everything except selling HealthClaw itself as
a competing product, with every release converting to MIT after two years)
**and/or an open-core model** (enterprise capabilities such as managed
hosting, large-scale multi-tenant administration, SSO/SCIM, compliance
reporting packages, and SLAs offered commercially).

Two commitments regardless:

1. **Any version released under MIT stays MIT forever.** A license change
   would only apply to releases made after the change.
2. The guardrail core — PHI redaction, audit, step-up authorization,
   human-in-the-loop, tenant isolation, disclaimers, and the conformance
   harness — stays freely available for patients, clinicians, researchers,
   and the organizations that serve them.

Commercial questions or partnership interest: **license@healthclaw.io**.

Contributions are DCO-signed (`git commit -s`) so licensing stays clean —
see [CONTRIBUTING.md](CONTRIBUTING.md). Vendored components keep their own
licenses and notices (e.g. `src/ktc/` is MIT from
jmandel/kill-the-clipboard-skill).
