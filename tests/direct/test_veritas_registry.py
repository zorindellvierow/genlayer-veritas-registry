"""Direct-mode tests for Veritas Registry.

Runs fully in-memory (no server, no Docker, no network) via genlayer-test
direct mode. Web fetches and LLM calls are mocked, so the consensus logic
— leader proposal + independent validator re-run — is exercised
deterministically.

Note on direct-mode semantics: a contract call executes the LEADER path and
captures the validator function; consensus itself is checked explicitly with
direct_vm.run_validator(). On the real network the validator committee runs
automatically and a rejected leader result never reaches state.
"""

import json

import pytest

CONTRACT = "contracts/veritas_registry.py"
MIN_STAKE = 100_000_000_000_000_000  # 0.1 GEN
EVIDENCE_1 = "https://example.com/evidence-1"
EVIDENCE_2 = "https://example.com/evidence-2"
COUNTER_EVIDENCE = "https://example.com/counter-evidence"

CLAIM_TEXT = "The Eiffel Tower is 330 metres tall including antennas."


def _judge(verdict, confidence, reasoning="mocked reasoning"):
    return json.dumps(
        {
            "verdict": verdict,
            "confidence": confidence,
            "reasoning": reasoning,
            "evidence_summary": "mocked evidence summary",
        }
    )


def _mock_supporting_evidence(direct_vm):
    direct_vm.mock_web(
        r".*example\.com/evidence-1.*",
        {"status": 200, "body": "The Eiffel Tower is 330 metres tall."},
    )
    direct_vm.mock_web(
        r".*example\.com/evidence-2.*",
        {"status": 200, "body": "Official height: 330 m with antennas."},
    )


def _mock_judge(direct_vm, verdict, confidence):
    direct_vm.mock_llm(r".*fact-checking judge.*", _judge(verdict, confidence))


def _deploy(direct_vm, direct_deploy):
    return direct_deploy(CONTRACT, MIN_STAKE)


def _register(direct_vm, contract, sender, value=0):
    direct_vm.sender = sender
    direct_vm.value = value
    claim_id = contract.register_claim(
        CLAIM_TEXT, EVIDENCE_1, EVIDENCE_2, ""
    )
    direct_vm.value = 0
    return claim_id


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------



def _addr(a) -> str:
    """0x-hex form of a direct-mode address (bytes or Address object —
    the form Address(str) accepts)."""
    raw = a if isinstance(a, bytes) else a.as_bytes
    return "0x" + raw.hex()


def test_register_claim(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_vm, direct_deploy)
    claim_id = _register(direct_vm, contract, direct_alice)
    assert claim_id == 0
    assert contract.claim_count() == 1
    assert contract.get_status(0) == "pending"
    assert contract.get_verdict(0) == "unverified"
    assert contract.is_verified_true(0) is False
    assert contract.get_reputation(_addr(direct_alice)) == 0


def test_register_with_stake_records_stake(
    direct_vm, direct_deploy, direct_alice
):
    contract = _deploy(direct_vm, direct_deploy)
    stake = 5 * MIN_STAKE
    _register(direct_vm, contract, direct_alice, value=stake)
    assert contract.get_claim(0).stake == stake


def test_register_rejects_empty_text(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_vm, direct_deploy)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("claim text is empty"):
        contract.register_claim("", EVIDENCE_1, "", "")


def test_register_rejects_no_evidence(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_vm, direct_deploy)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("at least one evidence URL"):
        contract.register_claim(CLAIM_TEXT, "", "", "")


def test_get_missing_claim_reverts(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_vm, direct_deploy)
    with direct_vm.expect_revert("claim does not exist"):
        contract.get_claim(99)


# ----------------------------------------------------------------------
# Verification (leader path + reputation + counters)
# ----------------------------------------------------------------------


def test_verify_true(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_vm, direct_deploy)
    _register(direct_vm, contract, direct_alice)
    _mock_supporting_evidence(direct_vm)
    _mock_judge(direct_vm, "true", 90)
    direct_vm.sender = direct_alice
    assert contract.verify_claim(0) == "true"
    assert contract.get_status(0) == "verified"
    assert contract.get_verdict(0) == "true"
    assert contract.is_verified_true(0) is True
    assert contract.get_reputation(_addr(direct_alice)) == 1
    stats = json.loads(contract.get_stats())
    assert stats["verified_true"] == 1
    assert stats["total_claims"] == 1


def test_verify_false(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_vm, direct_deploy)
    _register(direct_vm, contract, direct_alice)
    _mock_supporting_evidence(direct_vm)
    _mock_judge(direct_vm, "false", 85)
    assert contract.verify_claim(0) == "false"
    assert contract.get_reputation(_addr(direct_alice)) == -1
    stats = json.loads(contract.get_stats())
    assert stats["verified_false"] == 1


def test_verify_uncertain_keeps_reputation(
    direct_vm, direct_deploy, direct_alice
):
    contract = _deploy(direct_vm, direct_deploy)
    _register(direct_vm, contract, direct_alice)
    _mock_supporting_evidence(direct_vm)
    _mock_judge(direct_vm, "uncertain", 40)
    assert contract.verify_claim(0) == "uncertain"
    assert contract.get_reputation(_addr(direct_alice)) == 0
    stats = json.loads(contract.get_stats())
    assert stats["verified_uncertain"] == 1


def test_verify_twice_reverts(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_vm, direct_deploy)
    _register(direct_vm, contract, direct_alice)
    _mock_supporting_evidence(direct_vm)
    _mock_judge(direct_vm, "true", 90)
    contract.verify_claim(0)
    with direct_vm.expect_revert("not pending"):
        contract.verify_claim(0)


def test_fetch_failure_still_judges_uncertain(
    direct_vm, direct_deploy, direct_alice
):
    """Unmocked evidence URLs must not crash verification: the fetch
    failure becomes evidence text and the judge returns uncertain."""
    contract = _deploy(direct_vm, direct_deploy)
    _register(direct_vm, contract, direct_alice)
    # No web mocks at all: fetches fail inside _fetch_evidence.
    _mock_judge(direct_vm, "uncertain", 30)
    assert contract.verify_claim(0) == "uncertain"
    assert contract.get_status(0) == "verified"


# ----------------------------------------------------------------------
# Consensus: the validator function (Equivalence Principle)
# ----------------------------------------------------------------------


def test_validator_accepts_agreeing_leader(
    direct_vm, direct_deploy, direct_alice
):
    contract = _deploy(direct_vm, direct_deploy)
    _register(direct_vm, contract, direct_alice)
    _mock_supporting_evidence(direct_vm)
    _mock_judge(direct_vm, "true", 90)
    contract.verify_claim(0)
    assert direct_vm.run_validator() is True


def test_validator_rejects_wrong_verdict(
    direct_vm, direct_deploy, direct_alice
):
    """Leader claims false while the validator's independent run says
    true -> the verdict fields differ -> reject."""
    contract = _deploy(direct_vm, direct_deploy)
    _register(direct_vm, contract, direct_alice)
    _mock_supporting_evidence(direct_vm)
    _mock_judge(direct_vm, "true", 90)
    contract.verify_claim(0)
    lying_leader = json.loads(_judge("false", 90))
    assert direct_vm.run_validator(leader_result=lying_leader) is False


def test_validator_rejects_confidence_drift(
    direct_vm, direct_deploy, direct_alice
):
    """Same verdict, but leader confidence 50 vs validator 90: drift 40
    exceeds the +/-15 tolerance -> reject."""
    contract = _deploy(direct_vm, direct_deploy)
    _register(direct_vm, contract, direct_alice)
    _mock_supporting_evidence(direct_vm)
    _mock_judge(direct_vm, "true", 90)
    contract.verify_claim(0)
    drifting_leader = json.loads(_judge("true", 50))
    assert direct_vm.run_validator(leader_result=drifting_leader) is False


def test_validator_accepts_confidence_within_tolerance(
    direct_vm, direct_deploy, direct_alice
):
    """Same verdict, leader confidence 80 vs validator 90: drift 10 is
    within +/-15 -> accept."""
    contract = _deploy(direct_vm, direct_deploy)
    _register(direct_vm, contract, direct_alice)
    _mock_supporting_evidence(direct_vm)
    _mock_judge(direct_vm, "true", 90)
    contract.verify_claim(0)
    close_leader = json.loads(_judge("true", 80))
    assert direct_vm.run_validator(leader_result=close_leader) is True


# ----------------------------------------------------------------------
# Challenges
# ----------------------------------------------------------------------


def _verify_as_true(direct_vm, contract):
    _mock_supporting_evidence(direct_vm)
    _mock_judge(direct_vm, "true", 90)
    contract.verify_claim(0)


def _challenge(direct_vm, contract, sender, value=MIN_STAKE):
    direct_vm.sender = sender
    direct_vm.value = value
    contract.challenge_claim(
        0, "The sources do not support the height claim.", COUNTER_EVIDENCE
    )
    direct_vm.value = 0


def test_challenge_requires_min_stake(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = _deploy(direct_vm, direct_deploy)
    _register(direct_vm, contract, direct_alice)
    _verify_as_true(direct_vm, contract)
    direct_vm.sender = direct_bob
    direct_vm.value = MIN_STAKE - 1
    with direct_vm.expect_revert("below minimum"):
        contract.challenge_claim(0, "disagree", COUNTER_EVIDENCE)
    direct_vm.value = 0


def test_challenge_by_submitter_reverts(
    direct_vm, direct_deploy, direct_alice
):
    contract = _deploy(direct_vm, direct_deploy)
    _register(direct_vm, contract, direct_alice)
    _verify_as_true(direct_vm, contract)
    direct_vm.sender = direct_alice
    direct_vm.value = MIN_STAKE
    with direct_vm.expect_revert("cannot challenge own claim"):
        contract.challenge_claim(0, "disagree", COUNTER_EVIDENCE)
    direct_vm.value = 0


def test_resolve_without_challenge_reverts(
    direct_vm, direct_deploy, direct_alice
):
    contract = _deploy(direct_vm, direct_deploy)
    _register(direct_vm, contract, direct_alice)
    _verify_as_true(direct_vm, contract)
    with direct_vm.expect_revert("no open challenge"):
        contract.resolve_challenge(0)


def test_challenge_overturn(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = _deploy(direct_vm, direct_deploy)
    _register(direct_vm, contract, direct_alice, value=2 * MIN_STAKE)
    _verify_as_true(direct_vm, contract)
    assert contract.get_reputation(_addr(direct_alice)) == 1
    _challenge(direct_vm, contract, direct_bob)
    assert contract.get_status(0) == "disputed"
    # Re-verification with counter-evidence now judges the claim false.
    # NOTE: mock_llm matches the FIRST registered mock, so the old "true"
    # judge must be cleared before re-mocking for the resolution round.
    direct_vm.clear_mocks()
    _mock_supporting_evidence(direct_vm)
    direct_vm.mock_web(
        r".*counter-evidence.*",
        {"status": 200, "body": "The tower is 312 m without antennas."},
    )
    _mock_judge(direct_vm, "false", 88)
    assert contract.resolve_challenge(0) == "false"
    assert contract.get_status(0) == "resolved"
    assert contract.get_verdict(0) == "false"
    assert contract.get_reputation(_addr(direct_bob)) == 1
    assert contract.get_reputation(_addr(direct_alice)) == 0  # +1 then -1
    stats = json.loads(contract.get_stats())
    assert stats["overturned"] == 1
    assert stats["verified_true"] == 0
    assert stats["verified_false"] == 1


def test_challenge_upheld(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = _deploy(direct_vm, direct_deploy)
    _register(direct_vm, contract, direct_alice, value=2 * MIN_STAKE)
    _verify_as_true(direct_vm, contract)
    _challenge(direct_vm, contract, direct_bob)
    direct_vm.clear_mocks()
    _mock_supporting_evidence(direct_vm)
    direct_vm.mock_web(
        r".*counter-evidence.*",
        {"status": 200, "body": "Unrelated page about Paris tourism."},
    )
    _mock_judge(direct_vm, "true", 92)
    assert contract.resolve_challenge(0) == "true"
    assert contract.get_status(0) == "verified"
    assert contract.get_verdict(0) == "true"
    assert contract.get_reputation(_addr(direct_bob)) == -1
    assert contract.get_reputation(_addr(direct_alice)) == 2  # +1 verify, +1 upheld
    stats = json.loads(contract.get_stats())
    assert stats["challenges_lost"] == 1
    assert stats["verified_true"] == 1


def test_challenge_uncertain_does_not_overturn(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    """A re-verdict of 'uncertain' is doubt, not disproof: the original
    verdict stands and the challenge fails."""
    contract = _deploy(direct_vm, direct_deploy)
    _register(direct_vm, contract, direct_alice)
    _verify_as_true(direct_vm, contract)
    _challenge(direct_vm, contract, direct_bob)
    direct_vm.clear_mocks()
    _mock_supporting_evidence(direct_vm)
    direct_vm.mock_web(
        r".*counter-evidence.*",
        {"status": 200, "body": "Unrelated page about Paris tourism."},
    )
    _mock_judge(direct_vm, "uncertain", 45)
    assert contract.resolve_challenge(0) == "uncertain"
    assert contract.get_status(0) == "verified"
    assert contract.get_verdict(0) == "true"
    assert contract.get_reputation(_addr(direct_bob)) == -1
