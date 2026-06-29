# runspec-console — Security: Executive Brief

**One-page summary for decision-makers.** Full detail and source-level evidence:
[`security-assessment.md`](security-assessment.md). Assessed: console 0.148.0 /
core 0.47.0 (commit `fcf79dc`), 2026-06-29. Method: source-code architecture
review — *not* a third-party penetration test.

## What it is

A **desktop app** an operator installs on their own Windows workstation to run,
schedule, and monitor small declared command-line tools — locally and across a
fleet of **remote servers over SSH** — optionally with an **AI assistant** that
proposes actions for the operator to approve.

## Why it is safe to evaluate for rollout — the three load-bearing facts

1. **It's a client, not a server.** The console makes only *outbound* connections
   (SSH out to servers, HTTPS out to your chosen AI provider). It opens **no
   inbound network ports**; its UI is served only to itself on `127.0.0.1`. A
   network attacker has essentially nothing to reach.
2. **AI cannot act unilaterally.** Default posture is **"confirm": the AI may read
   freely but must get explicit human approval before changing or running
   anything.** The approval gate is enforced in code, **fails closed** (a timeout
   = deny), and **cannot be bypassed by the AI model** — a manipulated model can
   ask, but only a human can authorise. The operator can also edit a proposed
   action before approving it.
3. **Secrets are protected.** Credential secrets live in the **Windows Credential
   Manager** (never in config files, never synced). They reach tools via
   environment variables (**never the command line**, so they don't show in
   process lists) and are **never sent to the AI provider**.

## Risk posture at a glance

| Inbound network exposure | AI governance | Secret handling | Auditability | Data egress |
|---|---|---|---|---|
| **Very low** | **Good (safe-by-default)** | **Good** | **Good (local)** | **You control it** |

## The three things to manage (and how)

| Watch-item | Why it matters | Control you put in place |
|---|---|---|
| **The app trusts the logged-in Windows user** (no second in-app login) | Whoever is at an unlocked session has the operator's access, including SSH to fleet hosts | BitLocker, screen-lock GPO, passphrase-protected SSH keys |
| **Powerful features are configuration-gated, not removed** (e.g. unattended automation) | Safe defaults exist, but an operator *can* configure broad behaviour | Mandatory allowlists on automation; the fleet-wide **denylist** backstop; treat automation config as a privileged change |
| **AI carries standard prompt-injection risk** from untrusted email/chat | Malicious content could try to steer the assistant | The confirm-gate is the backstop (holds even against a manipulated model); point the AI at an **internal gateway** if traffic must stay in-perimeter |

## Where your data goes

**Nothing leaves the machine to any third party by default except the AI provider
you explicitly configure** — and to that provider it sends prompts (including
infrastructure *names*) but **never secrets**. No telemetry, no analytics, no
phone-home. AI traffic can be routed to an in-perimeter gateway (e.g. AWS Bedrock
in your own account) to satisfy data-residency policy.

## Honest gaps (not blockers, but own them)

- Distribution has **no artifact signing or SBOM**, and dependencies use version
  *floors* — install from a **pinned internal package mirror**.
- Audit logs are **local and not tamper-proof** — **forward them to your SIEM**.
- The optional `runspec-room` server (only if you adopt help-desk "rooms") is a
  network service and needs standard server hardening (TLS, reverse proxy, rate
  limiting) — assess separately.

## Recommendation

The architecture is **defence-in-depth and safe-by-default**, and every control
claim is verifiable in source (see the full report's verification appendix).
Suitable to proceed to a **controlled pilot** with the workstation, SSH, and
automation-governance controls above, and to commission an **independent
penetration test + privacy/DPIA review** (scoped using the full assessment) before
fleet-wide deployment.
