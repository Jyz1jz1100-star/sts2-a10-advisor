"""The promotion gate must refuse a clause it was never given, not score its default.

Every V2 promotion record on disk predates ``boundary_wilson_95_low``, and reading that
field off a rehydrated payload yielded the dataclass default -- so five floor6 rejections
were reported as "boundary_wilson_95_low 0.0000 < required 0.9000", a number nobody
measured.  The same confusion in the other direction is worse: an absent
``defect_truncation_rate`` reads as zero truncation defects, which is a *pass*.
"""

from __future__ import annotations

import unittest

from training.config import PromotionConfig
from training.metrics import EvaluationMetrics
from training.promotion import decide_promotion

CLEAN_RECORD = {
    "schema_version": 4,
    "generated_at": "2026-09-20T00:00:00+00:00",
    "stage": "floor6",
    "split": "promotion",
    "scope": "simulator_act1",
    "experimental": False,
    "checkpoint": "checkpoints/step_000001000008.zip",
    "checkpoint_sha256": "0" * 64,
    "deterministic": True,
    "seed_count": 500,
    "seed_sha256": "1" * 64,
    "episodes": 500,
    "wins": 0,
    "campaign_clears": 0,
    # A single-act record carries neither field; presence is what distinguishes
    # "measured without campaign" from "the file predates the campaign label".
    "environment_version": None,
    "content_coverage": None,
    "win_rate": 0.0,
    "wilson_95_low": 0.0,
    "wilson_95_high": 0.0,
    "truncations": 500,
    "truncation_rate": 1.0,
    "illegal_actions": 0,
    "mean_steps": 400.0,
    "mean_return": 1.0,
    "mean_final_floor": 6.0,
    "max_final_floor": 6,
    "boundary_rate": 0.96,
    "boundary_wilson_95_low": 0.94,
    "boundary_wilson_95_high": 0.97,
    "boundary_hits": 480,
    "mean_final_hp_fraction": 0.5,
    "dead_end_reasons": {},
    "final_floor_histogram": {"6": 500},
    "unclassified_dead_ends": 0,
    "defect_truncation_rate": 0.02,
    "rejection_events": 0,
    "by_encounter": {},
    "by_act": {},
    "winning_seeds": [],
}

FLOOR6_GATE = PromotionConfig(
    min_episodes=500,
    min_win_rate=0.0,
    min_wilson_lower=0.0,
    max_truncation_rate=0.03,
    max_illegal_actions=0,
    min_boundary_rate=0.93,
    min_boundary_wilson_lower=0.90,
)


def decide(record: dict, requirements: PromotionConfig = FLOOR6_GATE):
    return decide_promotion(
        EvaluationMetrics.from_payload(record), requirements
    )


class AbsentMetricTests(unittest.TestCase):
    def test_a_record_carrying_every_gate_input_still_promotes(self) -> None:
        decision = decide(dict(CLEAN_RECORD))
        self.assertTrue(decision.promoted, decision.reasons)

    def test_the_absence_bookkeeping_is_not_part_of_the_on_disk_schema(self) -> None:
        metrics = EvaluationMetrics.from_payload(dict(CLEAN_RECORD))
        self.assertEqual(metrics.absent_metrics, frozenset())
        payload = metrics.to_dict()
        self.assertNotIn("absent_metrics", payload)
        self.assertEqual(EvaluationMetrics.from_payload(payload).absent_metrics, frozenset())

    def test_dropping_the_truncation_measurement_cannot_buy_a_pass(self) -> None:
        without_cap = {k: v for k, v in CLEAN_RECORD.items() if k != "defect_truncation_rate"}
        self.assertTrue(decide(dict(CLEAN_RECORD)).promoted)
        decision = decide(without_cap)
        self.assertFalse(decision.promoted)
        self.assertTrue(
            any("defect_truncation_rate" in reason and "not recorded" in reason
                for reason in decision.reasons),
            decision.reasons,
        )
        self.assertFalse(
            any("defect_truncation_rate 0.0000" in reason for reason in decision.reasons),
            f"a default was still scored as a measurement: {decision.reasons}",
        )

    def test_an_unmeasured_wilson_bound_is_refused_rather_than_read_as_zero(self) -> None:
        thinned = {
            k: v for k, v in CLEAN_RECORD.items()
            if k not in {"boundary_wilson_95_low", "boundary_rate"}
        }
        decision = decide(thinned)
        self.assertFalse(decision.promoted)
        self.assertFalse(
            any("0.0000" in reason for reason in decision.reasons),
            f"the gate compared a default against a threshold: {decision.reasons}",
        )
        named = {reason.split()[0] for reason in decision.reasons}
        self.assertEqual(
            named, {"boundary_rate", "boundary_wilson_95_low"}, decision.reasons
        )

    def test_a_clause_the_stage_never_requires_is_not_refused(self) -> None:
        # floor3 sets no Wilson bound, so its records' missing field must not become a
        # rejection -- otherwise the refusal would read as a gate that got stricter.
        relaxed = PromotionConfig(
            min_episodes=500,
            min_win_rate=0.0,
            min_wilson_lower=0.0,
            max_truncation_rate=0.03,
            max_illegal_actions=0,
            min_boundary_rate=0.90,
        )
        thinned = {k: v for k, v in CLEAN_RECORD.items() if k != "boundary_wilson_95_low"}
        decision = decide(thinned, relaxed)
        self.assertTrue(decision.promoted, decision.reasons)


if __name__ == "__main__":
    unittest.main()
