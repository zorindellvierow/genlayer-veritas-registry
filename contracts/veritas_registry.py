# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
Veritas Registry — a reusable on-chain claim-verification primitive.

Any application (prediction market, fact-checking feed, bounty adjudication,
content moderation, agent-to-agent commerce) can:

  1. register_claim()  — post a factual claim + up to 3 evidence URLs,
                         optionally staking GEN behind it;
  2. verify_claim()    — ask the validator set to fetch the evidence and
                         LLM-judge the claim true / false / uncertain;
  3. challenge_claim() — stake GEN to dispute a verdict, with a reason and
                         counter-evidence URL;
  4. resolve_challenge() — re-verify with ALL evidence (original + counter).
                         If the verdict flips, the challenger wins the stakes;
                         if it stands, the challenger loses their stake to
                         the submitter.

How consensus is used (this is the core of the primitive, not a thin
LLM wrapper):

  * The leader independently fetches every evidence URL with
    gl.nondet.web.get and asks an LLM to judge the claim, returning a
    STRUCTURED verdict: {verdict, confidence, reasoning, evidence_summary}.
  * Each validator INDEPENDENTLY re-runs the same fetch + judge task and
    compares only the decision fields (Equivalence Principle patterns 1+2
    from the GenLayer docs: partial field matching + numeric tolerance):
      - "verdict" must match exactly (true / false / uncertain);
      - "confidence" must agree within +/-15 points (LLM subjectivity);
      - "reasoning" / "evidence_summary" are stored but never compared —
        two LLMs will word them differently.
  * A validator that only checked the leader's JSON shape would NOT be
    consensus — every validator re-derives the answer from the sources.
  * Fetch failures are data, not crashes: a failed URL is recorded as
    "[FETCH FAILED]" evidence, so the judge can still return "uncertain"
    instead of the transaction dying.
  * Verdicts are readable by other contracts via get_verdict(),
    is_verified_true() and get_claim(), so Veritas can be composed into
    any adjudication flow.

Reputation: submitters gain +1 when a claim verifies true and -1 when it
verifies false; challengers gain +1 on a successful overturn and -1 on a
failed challenge. Reputation is on-chain state other contracts can read.

Stakes are real GEN (wei, 1 GEN = 10^18). Registration stake is optional;
a challenge requires at least `challenge_min_stake` (set at deploy time,
immutable). Payouts are emitted with _EOA(...).emit_transfer() and execute
on finalization.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from genlayer import *


# Confidence agreement tolerance (points on a 0-100 scale) between the
# leader's verdict and an independent validator's verdict.
CONFIDENCE_TOLERANCE = 15
# Max characters fetched from any single evidence URL (bounds prompt size).
MAX_EVIDENCE_CHARS = 4000


@gl.evm.contract_interface
class _EOA:
    """Minimal EVM interface used only to pay GEN to plain addresses."""

    class View:
        pass

    class Write:
        pass


@allow_storage
@dataclass
class Claim:
    id: u256
    submitter: Address
    text: str
    evidence_url_1: str
    evidence_url_2: str
    evidence_url_3: str
    status: str  # pending | verified | disputed | resolved
    verdict: str  # unverified | true | false | uncertain
    confidence: u256  # 0-100
    reasoning: str
    evidence_summary: str
    stake: u256
    challenge_stake: u256
    challenger: Address
    challenge_reason: str
    challenge_evidence_url: str
    verifications: u256
    created_at: u256
    updated_at: u256


class VeritasRegistry(gl.Contract):
    claim_counter: u256
    claims: TreeMap[u256, Claim]
    reputation: TreeMap[Address, i64]
    challenge_min_stake: u256
    owner: Address
    total_verified_true: u256
    total_verified_false: u256
    total_verified_uncertain: u256
    total_overturned: u256
    total_challenges_lost: u256

    def __init__(self, challenge_min_stake: u256):
        self.claim_counter = u256(0)
        self.challenge_min_stake = challenge_min_stake
        self.owner = gl.message.sender_address
        self.total_verified_true = u256(0)
        self.total_verified_false = u256(0)
        self.total_verified_uncertain = u256(0)
        self.total_overturned = u256(0)
        self.total_challenges_lost = u256(0)

    # ------------------------------------------------------------------
    # Internal helpers (deterministic)
    # ------------------------------------------------------------------

    def _now(self) -> u256:
        return u256(int(datetime.now(timezone.utc).timestamp()))

    def _get_claim(self, claim_id: u256) -> Claim:
        if claim_id >= self.claim_counter:
            raise gl.vm.UserError("claim does not exist")
        return self.claims[claim_id]

    def _add_reputation(self, who: Address, delta: i64) -> None:
        self.reputation[who] = self.reputation.get(who, i64(0)) + delta

    def _count_verdict(self, verdict: str, delta: int) -> None:
        # delta is +1 or -1; counters never go below zero by construction
        # (a verdict is only un-counted if it was counted before).
        if verdict == "true":
            self.total_verified_true = u256(
                int(self.total_verified_true) + delta
            )
        elif verdict == "false":
            self.total_verified_false = u256(
                int(self.total_verified_false) + delta
            )
        elif verdict == "uncertain":
            self.total_verified_uncertain = u256(
                int(self.total_verified_uncertain) + delta
            )

    def _parse_json(self, raw) -> dict:
        """Parse LLM output defensively.

        Accepts an already-parsed dict (exec_prompt with
        response_format='json', and direct-mode test mocks, both return
        one) or a string, stripping markdown fences if present."""
        if isinstance(raw, dict):
            return raw
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else ""
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < 0 or end <= start:
            raise gl.vm.UserError("judge returned no JSON object")
        return json.loads(text[start : end + 1])

    def _fetch_evidence(self, url: str) -> str:
        """Fetch one evidence URL. Failures become evidence text, never a
        crash — a missing source should push the verdict to "uncertain",
        not kill the transaction."""
        if url == "":
            return ""
        try:
            body = gl.nondet.web.get(url).body
            text = body.decode("utf-8", errors="replace")
            return text[:MAX_EVIDENCE_CHARS]
        except Exception:
            return "[FETCH FAILED: %s]" % url

    # ------------------------------------------------------------------
    # Consensus core: fetch evidence + LLM judge, verified independently
    # ------------------------------------------------------------------

    def _judge_claim(
        self, text: str, urls: list, challenge_note: str
    ) -> dict:
        """Run the leader/validator consensus round for one claim.

        leader_fn: fetch all evidence URLs, then LLM-judge the claim and
        return a structured dict.
        validator_fn: independently re-run leader_fn and compare ONLY the
        decision fields — verdict must match exactly, confidence within
        +/- CONFIDENCE_TOLERANCE. Reasoning text is stored, never compared.
        """
        sources = []
        for i in range(len(urls)):
            sources.append((i, urls[i]))

        def leader_fn():
            blocks = []
            for idx, url in sources:
                if url == "":
                    continue
                evidence = self._fetch_evidence(url)
                blocks.append(
                    "[SOURCE %d: %s]\n%s" % (idx + 1, url, evidence)
                )
            evidence_text = "\n\n".join(blocks)
            if evidence_text == "":
                evidence_text = "[NO EVIDENCE PROVIDED]"
            prompt = (
                "You are a fact-checking judge. Decide whether the CLAIM "
                "below is supported by the EVIDENCE.\n\n"
                "CLAIM: " + text + "\n\n"
                "EVIDENCE (fetched from the web; failures are marked):\n"
                + evidence_text + "\n\n"
                + challenge_note
                + "RULES:\n"
                "- Judge ONLY from the evidence above. Do not use outside knowledge.\n"
                '- "true" = the evidence directly supports the claim.\n'
                '- "false" = the evidence directly contradicts the claim.\n'
                '- "uncertain" = the evidence is missing, inconclusive, or does not address the claim.\n'
                "- Confidence 0-100: how strongly the evidence supports your verdict.\n"
                "- Keep reasoning to 2-4 sentences, quoting the decisive fact.\n\n"
                "Return ONLY valid JSON (no markdown, no extra text):\n"
                '{"verdict": "true|false|uncertain", "confidence": <0-100>, '
                '"reasoning": "...", "evidence_summary": "..."}'
            )
            response = gl.nondet.exec_prompt(prompt)
            result = self._parse_json(response)
            if result.get("verdict") not in ("true", "false", "uncertain"):
                raise gl.vm.UserError("judge returned invalid verdict")
            confidence = int(result.get("confidence", 0))
            if confidence < 0 or confidence > 100:
                raise gl.vm.UserError("judge returned invalid confidence")
            return {
                "verdict": result["verdict"],
                "confidence": confidence,
                "reasoning": str(result.get("reasoning", "")),
                "evidence_summary": str(result.get("evidence_summary", "")),
            }

        def validator_fn(leader_result) -> bool:
            if not isinstance(leader_result, gl.vm.Return):
                return False
            try:
                own = leader_fn()
            except Exception:
                return False
            leader = leader_result.calldata
            try:
                if leader["verdict"] != own["verdict"]:
                    return False
                diff = leader["confidence"] - own["confidence"]
                if diff < 0:
                    diff = -diff
                return diff <= CONFIDENCE_TOLERANCE
            except Exception:
                return False

        return gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

    # ------------------------------------------------------------------
    # Public writes
    # ------------------------------------------------------------------

    @gl.public.write.payable
    def register_claim(
        self,
        text: str,
        evidence_url_1: str,
        evidence_url_2: str,
        evidence_url_3: str,
    ) -> u256:
        """Register a factual claim. Stake (msg value) is optional; any GEN
        sent is locked as backing for the claim and can be won by a
        successful challenger."""
        if text == "":
            raise gl.vm.UserError("claim text is empty")
        if evidence_url_1 == "" and evidence_url_2 == "" and evidence_url_3 == "":
            raise gl.vm.UserError("at least one evidence URL is required")
        claim_id = self.claim_counter
        self.claim_counter = self.claim_counter + u256(1)
        now = self._now()
        self.claims[claim_id] = Claim(
            id=claim_id,
            submitter=gl.message.sender_address,
            text=text,
            evidence_url_1=evidence_url_1,
            evidence_url_2=evidence_url_2,
            evidence_url_3=evidence_url_3,
            status="pending",
            verdict="unverified",
            confidence=u256(0),
            reasoning="",
            evidence_summary="",
            stake=gl.message.value,
            challenge_stake=u256(0),
            challenger=Address(
                "0x0000000000000000000000000000000000000000"
            ),
            challenge_reason="",
            challenge_evidence_url="",
            verifications=u256(0),
            created_at=now,
            updated_at=now,
        )
        return claim_id

    @gl.public.write
    def verify_claim(self, claim_id: u256) -> str:
        """Run consensus verification on a pending claim. Anyone may call
        it; the validator set does the work. Returns the verdict."""
        claim = self._get_claim(claim_id)
        if claim.status != "pending":
            raise gl.vm.UserError("claim is not pending verification")
        result = self._judge_claim(
            claim.text,
            [claim.evidence_url_1, claim.evidence_url_2, claim.evidence_url_3],
            "",
        )
        claim.verdict = result["verdict"]
        claim.confidence = u256(result["confidence"])
        claim.reasoning = result["reasoning"]
        claim.evidence_summary = result["evidence_summary"]
        claim.status = "verified"
        claim.verifications = claim.verifications + u256(1)
        claim.updated_at = self._now()
        self.claims[claim_id] = claim
        self._count_verdict(result["verdict"], 1)
        if result["verdict"] == "true":
            self._add_reputation(claim.submitter, i64(1))
        elif result["verdict"] == "false":
            self._add_reputation(claim.submitter, i64(-1))
        print(
            "veritas: claim %d verified as %s (confidence %d)"
            % (int(claim_id), result["verdict"], result["confidence"])
        )
        return result["verdict"]

    @gl.public.write.payable
    def challenge_claim(
        self, claim_id: u256, reason: str, counter_evidence_url: str
    ) -> None:
        """Dispute a verified verdict. Requires at least
        challenge_min_stake GEN, locked until resolution. Only one open
        challenge per claim at a time."""
        claim = self._get_claim(claim_id)
        if claim.status != "verified":
            raise gl.vm.UserError("only a verified claim can be challenged")
        if gl.message.sender_address == claim.submitter:
            raise gl.vm.UserError("submitter cannot challenge own claim")
        if gl.message.value < self.challenge_min_stake:
            raise gl.vm.UserError("challenge stake below minimum")
        if reason == "":
            raise gl.vm.UserError("challenge reason is empty")
        claim.status = "disputed"
        claim.challenger = gl.message.sender_address
        claim.challenge_reason = reason
        claim.challenge_evidence_url = counter_evidence_url
        claim.challenge_stake = gl.message.value
        claim.updated_at = self._now()
        self.claims[claim_id] = claim

    @gl.public.write
    def resolve_challenge(self, claim_id: u256) -> str:
        """Re-verify a disputed claim with ALL evidence (original URLs +
        challenger counter-evidence). Anyone may call it.

        Overturn rule: the new verdict must be a decisive opposite
        (true <-> false). A re-verdict of "uncertain" does NOT overturn —
        doubt alone does not disprove the original verdict; the challenge
        fails and the original verdict stands.

        - Overturned: challenger wins challenge stake + submitter stake,
          status becomes "resolved" (terminal), challenger +1 rep,
          submitter -1 rep.
        - Upheld: challenger stake is paid to the submitter, status returns
          to "verified" (challengeable again), challenger -1 rep,
          submitter +1 rep."""
        claim = self._get_claim(claim_id)
        if claim.status != "disputed":
            raise gl.vm.UserError("claim has no open challenge")
        old_verdict = claim.verdict
        challenge_note = (
            "CHALLENGE: a challenger disputes this verdict.\n"
            "Challenger reason: " + claim.challenge_reason + "\n"
            "Weigh the counter-evidence below together with the original "
            "sources. Only overturn the verdict if the combined evidence "
            "clearly supports the opposite conclusion.\n\n"
        )
        urls = [
            claim.evidence_url_1,
            claim.evidence_url_2,
            claim.evidence_url_3,
            claim.challenge_evidence_url,
        ]
        result = self._judge_claim(claim.text, urls, challenge_note)
        new_verdict = result["verdict"]
        overturned = (
            new_verdict in ("true", "false") and new_verdict != old_verdict
        )
        claim.verdict = new_verdict
        claim.confidence = u256(result["confidence"])
        claim.reasoning = result["reasoning"]
        claim.evidence_summary = result["evidence_summary"]
        claim.verifications = claim.verifications + u256(1)
        claim.updated_at = self._now()
        if overturned:
            self._count_verdict(old_verdict, -1)
            self._count_verdict(new_verdict, 1)
            self.total_overturned = self.total_overturned + u256(1)
            self._add_reputation(claim.challenger, i64(1))
            self._add_reputation(claim.submitter, i64(-1))
            payout = claim.challenge_stake + claim.stake
            claim.stake = u256(0)
            claim.challenge_stake = u256(0)
            claim.status = "resolved"
            self.claims[claim_id] = claim
            if payout > u256(0):
                _EOA(claim.challenger).emit_transfer(value=payout)
            print(
                "veritas: claim %d OVERTURNED %s -> %s"
                % (int(claim_id), old_verdict, new_verdict)
            )
        else:
            self.total_challenges_lost = self.total_challenges_lost + u256(1)
            self._add_reputation(claim.challenger, i64(-1))
            self._add_reputation(claim.submitter, i64(1))
            lost = claim.challenge_stake
            claim.challenge_stake = u256(0)
            claim.verdict = old_verdict
            claim.status = "verified"
            self.claims[claim_id] = claim
            if lost > u256(0):
                _EOA(claim.submitter).emit_transfer(value=lost)
            print(
                "veritas: claim %d challenge FAILED, verdict stands: %s"
                % (int(claim_id), old_verdict)
            )
        return new_verdict

    # ------------------------------------------------------------------
    # Public views (readable by users and by other contracts)
    # ------------------------------------------------------------------

    @gl.public.view
    def get_claim(self, claim_id: u256) -> Claim:
        return self._get_claim(claim_id)

    @gl.public.view
    def get_verdict(self, claim_id: u256) -> str:
        return self._get_claim(claim_id).verdict

    @gl.public.view
    def get_status(self, claim_id: u256) -> str:
        return self._get_claim(claim_id).status

    @gl.public.view
    def is_verified_true(self, claim_id: u256) -> bool:
        claim = self._get_claim(claim_id)
        return claim.status in ("verified", "resolved") and (
            claim.verdict == "true"
        )

    @gl.public.view
    def get_reputation(self, who: str) -> int:
        return self.reputation.get(Address(who), i64(0))

    @gl.public.view
    def claim_count(self) -> int:
        return self.claim_counter

    @gl.public.view
    def get_stats(self) -> str:
        return json.dumps(
            {
                "total_claims": int(self.claim_counter),
                "verified_true": int(self.total_verified_true),
                "verified_false": int(self.total_verified_false),
                "verified_uncertain": int(self.total_verified_uncertain),
                "overturned": int(self.total_overturned),
                "challenges_lost": int(self.total_challenges_lost),
            },
            sort_keys=True,
        )
