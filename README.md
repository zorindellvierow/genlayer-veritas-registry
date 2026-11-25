# Veritas Registry

A reusable **on-chain claim-verification primitive** for GenLayer
Intelligent Contracts.

Any application — a prediction market, a fact-checking feed, a bounty
adjudication flow, content moderation, or agent-to-agent commerce — can
register a factual claim with evidence URLs, get a consensus verdict from
GenLayer's AI validators, and let anyone challenge that verdict with a
stake. Verdicts are readable by other contracts, so Veritas composes into
any adjudication flow.

- Contract: [`contracts/veritas_registry.py`](contracts/veritas_registry.py)
- Network: GenLayer **Bradbury testnet** (chain ID 4221) — see
  [Deployment](#deployment)
- License: MIT

## What it does

| Method | Who | What happens |
|---|---|---|
| `register_claim(text, url1, url2, url3)` | anyone (payable, stake optional) | Stores a claim as `pending` with up to 3 evidence URLs. Any GEN sent is locked as backing stake. Returns the claim id. |
| `verify_claim(claim_id)` | anyone | Runs consensus verification. Sets `status = verified`, `verdict ∈ {true, false, uncertain}`, `confidence 0–100`, plus the leader's reasoning and evidence summary. |
| `challenge_claim(claim_id, reason, counter_url)` | anyone except the submitter (payable, ≥ min stake) | Opens a dispute: `status = disputed`, challenger stake locked. |
| `resolve_challenge(claim_id)` | anyone | Re-verifies with **all** evidence (original + counter-evidence). If the verdict flips, the challenger wins the stakes; if it stands, the challenger loses their stake to the submitter. |
| `get_claim / get_verdict / get_status / is_verified_true / get_reputation / claim_count / get_stats` | anyone (views) | Read state. `is_verified_true()` is the one-liner other contracts call. |

State machine: `pending → verified → disputed → verified` (challenge
fails) or `disputed → resolved` (verdict overturned, terminal).

## How consensus is used

This is the core of the primitive — not a thin LLM wrapper.

1. **Leader proposes.** The leader fetches every evidence URL with
   `gl.nondet.web.get` and asks an LLM to judge the claim, returning a
   **structured** verdict:
   `{"verdict": "true|false|uncertain", "confidence": 0–100, "reasoning": "...", "evidence_summary": "..."}`.
   The prompt forbids outside knowledge: only the fetched evidence counts.
2. **Validators independently re-derive.** Each validator re-runs the
   same fetch + judge task and compares **only the decision fields**
   (Equivalence Principle patterns 1 + 2 from the GenLayer docs — partial
   field matching + numeric tolerance):
   - `verdict` must match **exactly**;
   - `confidence` must agree within **±15 points** (LLM subjectivity);
   - `reasoning` / `evidence_summary` are stored but **never compared** —
     two LLMs will word them differently.
   
   A validator that only checked the leader's JSON shape would not be
   consensus. Every validator re-derives the answer from the sources.
3. **Overturn rule.** On a challenge, the verdict flips only on a decisive
   opposite (`true ↔ false`). A re-verdict of `uncertain` is doubt, not
   disproof — the original verdict stands and the challenge fails.
4. **Fetch failures are data, not crashes.** A URL that fails to load is
   recorded as `[FETCH FAILED: <url>]` evidence, so the judge can return
   `uncertain` instead of the transaction dying.

## State design

```python
Claim (stored per claim id)
  id, submitter, text
  evidence_url_1/2/3        # up to 3 sources (fixed slots, no nested collections)
  status                    # pending | verified | disputed | resolved
  verdict                   # unverified | true | false | uncertain
  confidence                # 0-100
  reasoning, evidence_summary
  stake, challenge_stake    # GEN in wei locked in the contract
  challenger, challenge_reason, challenge_evidence_url
  verifications             # how many consensus rounds ran
  created_at, updated_at    # tx timestamps (deterministic)

Registry
  claims: TreeMap[u256, Claim]
  reputation: TreeMap[Address, i64]   # +1 true / -1 false (submitter);
                                      # +1 won / -1 lost challenge
  challenge_min_stake: u256          # set once at deploy, immutable
  counters: verified true/false/uncertain, overturned, challenges lost
```

Stakes are real GEN (1 GEN = 10¹⁸ wei). Registration stake is optional;
a challenge requires at least `challenge_min_stake`. Payouts use
`emit_transfer()` to plain addresses and execute on finalization.

## Development

Requires Python 3.11+ and Node 18+.

```bash
python3 -m venv venv && ./venv/bin/pip install genvm-linter genlayer-test
npm install -g genlayer@0.40.1
```

```bash
# Lint (AST safety checks + SDK semantic validation)
./venv/bin/genvm-lint check contracts/veritas_registry.py

# Direct-mode tests: in-memory, mocked web + LLM, no server needed.
# NOTE: stock genlayer-test 0.30.1 cannot run these — see
# scripts/patch_gltest.py (upstream version skew, details in Test report).
python scripts/patch_gltest.py
./venv/bin/gltest tests/direct/ -v

# Integration tests (need a funded account + network; see below)
./venv/bin/gltest tests/integration/ -v -s --network testnet_bradbury
```

Direct-mode tests cover: registration (incl. stake + input validation),
all three verdicts, reputation and counters, the validator function
(accept on agreement, reject on wrong verdict, reject on confidence drift
beyond ±15, accept within tolerance), fetch-failure handling, and the
full challenge lifecycle (overturn, upheld, uncertain-does-not-overturn).

## Deployment

```bash
# 1. Point the CLI at Bradbury testnet (chain ID 4221)
genlayer network set testnet-bradbury

# 2. Create a throwaway test account (never use a mainnet wallet)
genlayer account create --name veritas-test

# 3. Fund it from the testnet faucet (100 GEN, once per week)
#    https://testnet-faucet.genlayer.foundation/
#    Faucet sign-in is via GitHub OAuth.

# 4. Deploy (constructor arg = min challenge stake in wei; 0.1 GEN here)
genlayer deploy --contract contracts/veritas_registry.py \
  --args 100000000000000000
```

Deployed contract (Bradbury testnet): **TBD — see
[`submission-draft.md`](submission-draft.md)** (deployment pending faucet
funding of the throwaway test wallet).

Verify on the explorer: `https://scan.testnet-chain.genlayer.com/`

## Usage (example calls via CLI)

```bash
# Register a claim (no stake)
genlayer write <CONTRACT> register_claim \
  --args "The Eiffel Tower is 330 metres tall including antennas." \
         "https://example.com/evidence-1" "https://example.com/evidence-2" ""

# Verify it (consensus round)
genlayer write <CONTRACT> verify_claim --args 0

# Read the verdict (also callable from another Intelligent Contract)
genlayer call <CONTRACT> get_verdict --args 0
genlayer call <CONTRACT> is_verified_true --args 0

# Challenge it — payable, so use genlayer-js (the CLI `write` command in
# v0.40.1 exposes no --value flag) or the Studio write UI:
#   value: 0.1 GEN = 100000000000000000n wei
import { createClient, createAccount } from 'genlayer-js';
import { testnetBradbury } from 'genlayer-js/chains';
const client = createClient({ chain: testnetBradbury, account: createAccount() });
const write = {
  address: '<CONTRACT>', functionName: 'challenge_claim',
  args: [0n, 'The sources measure without antennas.',
         'https://example.com/counter'],
  value: 100000000000000000n,
};
const estimate = await client.estimateTransactionFeesForWrite(write);
const txId = await client.writeContract({ ...write, fees: {
  distribution: estimate.distribution, feeValue: estimate.feeValue } });

# Resolve the challenge (fresh consensus round with all evidence)
genlayer write <CONTRACT> resolve_challenge --args 0
```

## Design decisions and limits

- **Max 3 evidence URLs per claim** (fixed string slots). This keeps
  storage types flat and the prompt bounded; a claim needing more sources
  should link one index/landing page as its evidence URL.
- **Confidence tolerance ±15.** Two LLMs judging the same evidence should
  land within ~15 points; a larger drift means genuine disagreement and
  the leader is rejected.
- **`resolved` is terminal.** After an overturn, the claim cannot be
  challenged again — the challenge round already re-verified it with all
  evidence.
- **No frontend.** This is a primitive, not an app: the deliverable is
  the contract + tests + docs. A dApp integrates via `genlayer-js` /
  `genlayer-py` calls shown above.
- **Runner version.** The contract pins the documented `py-genlayer`
  runner (`1jb45aa8…`). The linter notes a newer runner exists; the pinned
  one is kept deliberately for stability — it is the runner the docs'
  examples are written for (`from genlayer import *` + `gl.*` works).
  Known skew: the current genlayer-test (0.30.1) and genlayer-py (0.19.1)
  were built for the v0.3 layout, so direct tests need
  `scripts/patch_gltest.py`, and on local GLSim (v0.6.0-rc17) method calls
  against a legacy-runner deployment fail with "No method in calldata"
  (see Test report). The v0.3 runner path is not yet a drop-in
  replacement (its stdlib does not export `gl` the same way).

## Test report

- `genvm-lint check`: **passed** (lint + semantic validation; 11 methods:
  7 view, 4 write).
- Direct-mode tests (`gltest tests/direct/`): **20 tests, all passing**
  (run locally, mocked web/LLM, no network). Run
  `python scripts/patch_gltest.py` first — see below.
- Integration tests (`gltest tests/integration/`): written; **run once
  against local GLSim (v0.6.0-rc17) — method calls fail there** with the
  leader error `"No method in calldata"`: the client (genlayer-py 0.19.1)
  encodes a method call whose first calldata key decodes as `""` instead
  of `"method"`, and the legacy runner rejects it. Deploy itself succeeds
  on GLSim. Whether Bradbury behaves the same is **unverified** — the
  Bradbury run needs a funded account (see Deployment). If Bradbury shows
  the same error, the fix is to move the contract's `Depends` to a v0.3
  runner (see "Runner version" below) — that path is NOT yet validated:
  on GLSim the v0.3 runner failed at load with `name 'gl' is not defined`
  (the new stdlib does not export `gl` via `from genlayer import *`),
  so the contract would also need an import change for that runner.
- Bradbury deployment: **pending** faucet funding of the throwaway test
  wallet `0x9bddf8fa290e4ab4bb8481bfeac00b61e284667b`.

### Upstream version skew (why scripts/patch_gltest.py exists)

The contract pins the runner from the GenLayer docs
(`py-genlayer:1jb45aa8…`). That runner is a **legacy v0.2** runner whose
stdlib uses the old module layout (`genlayer.py.calldata`,
`genlayer.py.types`, `genlayer.gl.vm`, `gl.message` frozen at import).
genlayer-test 0.30.1's direct-mode loader was written for the **v0.3**
layout (`genlayer.calldata`, `genlayer.types`, `genlayer.vm`, live
`genlayer.message`). On a stock install every direct test fails at
contract load ("unexpected end of memory" — the message-context injection
silently no-ops) and per-call sender/value changes are silently ignored.
`scripts/patch_gltest.py` applies 8 minimal fallbacks to the installed
genlayer-test package (assertions fail loudly if the installed version
differs from 0.30.1). The contract itself is untouched by the script.
Verified reproducible: a fresh venv (`pip install -r
requirements-dev.txt`, run the script) passes all 20 direct tests.

One real contract bug was found and fixed while testing: `_parse_json`
now accepts an already-parsed dict as well as a string, because
`exec_prompt(response_format='json')` and direct-mode mocks return a
dict.
