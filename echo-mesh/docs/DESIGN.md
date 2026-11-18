# Design

> **Superseded.** The 2026-11-17 Master Development Directive replaced the earlier spec. See PHASE0-REPORT.md; full design lands after approval.
> supplied when this scaffold was created. Replace this file with the spec.
> Spec section references (§4.8 tiers, §4.9 quotas, §5.8 brain distribution,
> §8 MVP, §9 layout, §12.3 Mac-alone, §13 dashboard) are UNVERIFIED.

Principles carried from the build brief:
- Invite-only; ed25519 node identities; mutual TLS.
- Two node tiers: `brain` (receives brain updates) and `compute` (never does).
- No node assumes another is present; partners are inventoried at startup.
- Schedule by measured throughput, never assumed hardware.
- Share limit per node; local work preempts shared work.
- Brain updates: versioned package, block-hashed, signed, atomic apply, auto rollback (Phase 2).
- Code updates go through the installer channel, not the mesh.
- No crypto/token. Open network is design-only.
