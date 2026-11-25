"""Integration tests for Veritas Registry (live network).

These run against a real GenLayer environment (Bradbury testnet by
default) with REAL web fetches and REAL LLM validators — no mocks.

Prerequisites:
  - A funded account private key in VERITAS_TEST_PRIVATE_KEY
    (Bradbury testnet GEN from https://testnet-faucet.genlayer.foundation/)
  - Run: gltest tests/integration/ -v -s --network testnet_bradbury

Status (25 Nov 2026): run once against local GLSim (v0.6.0-rc17). Deploy
succeeds, but method calls fail there with the leader error
"No method in calldata" (client calldata vs legacy runner mismatch —
see README Test report). Bradbury behaviour is unverified; this test is
the check to re-run once the throwaway wallet is funded.

The evidence URLs below point at stable public pages (Wikipedia) so the
verdict is deterministic enough for a live consensus round. Verdict
assertions are deliberately loose (true OR uncertain) because live LLM
judges may legitimately disagree on confidence wording; the hard
assertions are on state transitions, not on one exact verdict.
"""

import json

import pytest
from gltest import get_contract_factory
from gltest.assertions import tx_execution_succeeded

MIN_STAKE = 100_000_000_000_000_000  # 0.1 GEN

CLAIM_TEXT = "The Eiffel Tower is 330 metres tall including antennas."
EVIDENCE_1 = "https://en.wikipedia.org/wiki/Eiffel_Tower"


def test_register_verify_and_read():
    factory = get_contract_factory("VeritasRegistry")
    contract = factory.deploy(args=[MIN_STAKE])

    tx = contract.register_claim(
        args=[CLAIM_TEXT, EVIDENCE_1, "", ""]
    ).transact()
    assert tx_execution_succeeded(tx)

    assert contract.claim_count().call() == 1
    assert contract.get_status(args=[0]).call() == "pending"

    tx = contract.verify_claim(args=[0]).transact()
    assert tx_execution_succeeded(tx)

    verdict = contract.get_verdict(args=[0]).call()
    assert verdict in ("true", "false", "uncertain")
    assert contract.get_status(args=[0]).call() == "verified"
    stats = json.loads(contract.get_stats().call())
    assert stats["total_claims"] == 1
