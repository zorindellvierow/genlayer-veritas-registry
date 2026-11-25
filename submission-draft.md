# GenLayer Portal submission draft — Veritas Registry

DO NOT SUBMIT from here. Submission is the parent agent's job, after the
user scans his wallet barcode on portal.genlayer.foundation
(Builder → Intelligent Contracts).

- Referral code (bos): QINN5NTT — use when registering/submitting.
- Repo: https://github.com/zorindellvierow/genlayer-veritas-registry
- Contract: contracts/veritas_registry.py (VeritasRegistry, 11 methods)
- Testnet contract address (Bradbury): TBD — deployment pending faucet
  funding of throwaway wallet 0x9bddf8fa290e4ab4bb8481bfeac00b61e284667b.
  Evidence URL for the portal should be the repo URL (and the explorer
  link once deployed).

## Title (max 80 chars, 63 used)

Veritas Registry: on-chain claim verification with AI consensus

## Description (max 1000 chars, 966 used)

Veritas Registry is a reusable on-chain claim-verification primitive for
GenLayer. Anyone registers a factual claim with up to 3 evidence URLs and
an optional stake. Validators independently fetch the evidence
(gl.nondet.web) and LLM-judge it true/false/uncertain with structured
reasoning; the verdict is aggregated via the Equivalence Principle
(gl.vm.run_nondet_unsafe, leader + validator): verdict must match
exactly, confidence within +/-15, reasoning never compared. Challengers
stake to dispute a verdict; resolution re-verifies with original +
counter-evidence and only overturns on a decisive opposite verdict,
updating claim status and reputation; payouts use emit_transfer.
Verdicts are readable by other contracts (is_verified_true, get_verdict,
get_stats). Tested: genvm-lint passed; 20 direct-mode tests pass
(registration, all verdicts, validator accept/reject, full challenge
lifecycle). Integration test + Bradbury deployment pending faucet funding.
