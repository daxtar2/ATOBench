from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import random
from pathlib import Path
from typing import Any, Iterable

from .common import (
    GateError,
    ensure_output_available,
    load_jsonl,
    read_json_or_yaml,
    require_real_data_authorization,
    stage_manifest,
    utc_now,
    write_csv,
    write_json,
)


STATES = (
    "grounded_verification",
    "unsupported_closure",
    "unreported_verification",
    "unresolved_verification",
    "state_unavailable",
)
OBSERVED_STATES = STATES[:-1]
DIMENSIONS = ("verification_control", "stop_decision", "report_grounding")
BANDS = ("poor", "limited", "good", "excellent")
BAND_VALUES = (*BANDS, "unavailable")


def _unique(
    rows: list[dict[str, Any]], key: str, label: str
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = str(row.get(key) or "")
        if not value or value in result:
            raise GateError(f"statistics duplicate or missing {label} {key}: {value}")
        result[value] = row
    return result


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _stable_seed(base_seed: int, *parts: str) -> int:
    digest = hashlib.sha256("::".join(parts).encode()).digest()
    return base_seed ^ int.from_bytes(digest[:8], "big")


def _bootstrap_mean_ci(
    values: list[float],
    *,
    replicates: int,
    confidence: float,
    seed: int,
) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    rng = random.Random(seed)
    n = len(values)
    samples = []
    for _ in range(replicates):
        samples.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    alpha = 1 - confidence
    return _quantile(samples, alpha / 2), _quantile(samples, 1 - alpha / 2)


def _binomial_cdf(k: int, n: int, probability: float) -> float:
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    if probability <= 0:
        return 1.0
    if probability >= 1:
        return 0.0
    return sum(
        math.comb(n, index)
        * probability**index
        * (1 - probability) ** (n - index)
        for index in range(k + 1)
    )


def _solve_cdf(k: int, n: int, target: float) -> float:
    low, high = 0.0, 1.0
    for _ in range(80):
        middle = (low + high) / 2
        if _binomial_cdf(k, n, middle) > target:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def _clopper_pearson(
    successes: int, trials: int, confidence: float
) -> tuple[float | None, float | None]:
    if trials <= 0:
        return None, None
    alpha = 1 - confidence
    lower = (
        0.0
        if successes == 0
        else _solve_cdf(successes - 1, trials, 1 - alpha / 2)
    )
    upper = (
        1.0
        if successes == trials
        else _solve_cdf(successes, trials, alpha / 2)
    )
    return lower, upper


def _groups(
    rows: list[dict[str, Any]],
) -> Iterable[tuple[str, str, str | None, list[dict[str, Any]], bool]]:
    for aou in sorted({row["aou"] for row in rows}):
        subset = [row for row in rows if row["aou"] == aou]
        yield "aou", aou, None, subset, True
    for aou, model in sorted({(row["aou"], row["model"]) for row in rows}):
        subset = [
            row for row in rows if row["aou"] == aou and row["model"] == model
        ]
        yield "model_by_aou", aou, model, subset, False


def _load_bundle(path: str, cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if path not in cache:
        target = Path(path)
        if not target.exists():
            raise GateError(f"statistics source judgment bundle is missing: {path}")
        value = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise GateError(f"statistics source judgment bundle is invalid: {path}")
        cache[path] = value
    return cache[path]


def _episode_low_confidence(
    episode: dict[str, Any],
    dimension: str,
    cache: dict[str, dict[str, Any]],
) -> bool:
    projection = episode["dimensions"][dimension]
    bundle = _load_bundle(projection["source_bundle"]["path"], cache)
    if projection["final"]["method"] == "adjudicated":
        adjudication = bundle.get("adjudication_output")
        if not isinstance(adjudication, dict) or adjudication.get("confidence") is None:
            raise GateError("statistics adjudicated judgment has no final confidence")
        return adjudication.get("confidence") == "low"
    reviewers = bundle.get("reviewer_outputs")
    if not isinstance(reviewers, dict) or not reviewers:
        raise GateError("statistics non-adjudicated judgment has no reviewers")
    confidences = [value.get("confidence") for value in reviewers.values()]
    if any(value is None for value in confidences):
        raise GateError("statistics reviewer judgment has no confidence")
    return "low" in confidences


def _episode_major_disagreement(
    episode: dict[str, Any], dimension: str
) -> bool | None:
    value = episode["dimensions"][dimension].get("reviewer_score_difference")
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        raise GateError("statistics episode has invalid reviewer score difference")
    return value >= 3


def _state_distribution_rows(
    memberships: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pooled: list[dict[str, Any]] = []
    per_model: list[dict[str, Any]] = []
    for scope, aou, model, rows, _ in _groups(memberships):
        target = pooled if scope == "aou" else per_model
        for condition in ("C0", "C1"):
            counter = collections.Counter(
                row[f"{condition.lower()}_state"] for row in rows
            )
            record: dict[str, Any] = {
                "scope": scope,
                "aou": aou,
                "model": model,
                "condition": condition,
                "pair_n": len(rows),
                "state_observed_n": len(rows) - counter["state_unavailable"],
            }
            for state in STATES:
                record[f"{state}_n"] = counter[state]
                record[f"{state}_proportion"] = counter[state] / len(rows)
            target.append(record)
    return pooled, per_model


def _missingness_rows(
    memberships: list[dict[str, Any]],
    *,
    replicates: int,
    confidence: float,
    seed: int,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for scope, aou, model, rows, primary in _groups(memberships):
        patterns = collections.Counter(
            (row["c0_state_observed"], row["c1_state_observed"]) for row in rows
        )
        gain = patterns[(False, True)]
        loss = patterns[(True, False)]
        discordant = gain + loss
        exact_low, exact_high = _clopper_pearson(gain, discordant, confidence)
        deltas = [
            float(row["c1_state_observed"]) - float(row["c0_state_observed"])
            for row in rows
        ]
        boot_low, boot_high = (
            _bootstrap_mean_ci(
                deltas,
                replicates=replicates,
                confidence=confidence,
                seed=_stable_seed(seed, "availability", scope, aou, model or ""),
            )
            if primary
            else (None, None)
        )
        output.append(
            {
                "scope": scope,
                "aou": aou,
                "model": model,
                "pair_n": len(rows),
                "c0_observed_c1_observed_n": patterns[(True, True)],
                "c0_observed_c1_unavailable_n": loss,
                "c0_unavailable_c1_observed_n": gain,
                "c0_unavailable_c1_unavailable_n": patterns[(False, False)],
                "c0_observed_n": sum(row["c0_state_observed"] for row in rows),
                "c1_observed_n": sum(row["c1_state_observed"] for row in rows),
                "c0_observed_proportion": sum(
                    row["c0_state_observed"] for row in rows
                )
                / len(rows),
                "c1_observed_proportion": sum(
                    row["c1_state_observed"] for row in rows
                )
                / len(rows),
                "paired_availability_difference_c1_minus_c0": _mean(deltas),
                "paired_availability_difference_bootstrap_ci_low": boot_low,
                "paired_availability_difference_bootstrap_ci_high": boot_high,
                "discordant_n": discordant,
                "c1_observed_among_discordant_n": gain,
                "c1_observed_among_discordant_proportion": (
                    gain / discordant if discordant else None
                ),
                "discordant_proportion_exact_ci_low": exact_low,
                "discordant_proportion_exact_ci_high": exact_high,
                "primary_inferential_scope": primary,
            }
        )
    return output


def _transition_rows(
    memberships: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for scope, aou, model, rows, _ in _groups(memberships):
        complete = [row for row in rows if row["complete_state_pair"]]
        counter = collections.Counter(
            (row["c0_state"], row["c1_state"]) for row in complete
        )
        for c0_state in OBSERVED_STATES:
            for c1_state in OBSERVED_STATES:
                output.append(
                    {
                        "scope": scope,
                        "aou": aou,
                        "model": model,
                        "complete_pair_n": len(complete),
                        "excluded_incomplete_pair_n": len(rows) - len(complete),
                        "c0_state": c0_state,
                        "c1_state": c1_state,
                        "transition_n": counter[(c0_state, c1_state)],
                        "transition_proportion_of_complete": (
                            counter[(c0_state, c1_state)] / len(complete)
                            if complete
                            else None
                        ),
                    }
                )
    return output


def _retention_rows(
    memberships: list[dict[str, Any]],
    *,
    replicates: int,
    confidence: float,
    seed: int,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for scope, aou, model, rows, primary in _groups(memberships):
        target = [row for row in rows if row["c0_capability_target"]]
        observed = [
            row for row in target if row["c0_capability_outcome_observed"]
        ]
        retained = sum(
            row["c1_state"] == "grounded_verification" for row in observed
        )
        lost = len(observed) - retained
        missing = len(target) - len(observed)
        outcomes = [
            float(row["c1_state"] == "grounded_verification") for row in observed
        ]
        boot_low, boot_high = (
            _bootstrap_mean_ci(
                outcomes,
                replicates=replicates,
                confidence=confidence,
                seed=_stable_seed(seed, "retention", scope, aou, model or ""),
            )
            if primary
            else (None, None)
        )
        exact_low, exact_high = _clopper_pearson(
            retained, len(observed), confidence
        )
        output.append(
            {
                "scope": scope,
                "aou": aou,
                "model": model,
                "capability_target_n": len(target),
                "outcome_observed_n": len(observed),
                "outcome_missing_n": missing,
                "outcome_missing_proportion": (
                    missing / len(target) if target else None
                ),
                "retained_n": retained,
                "loss_n": lost,
                "observed_grounded_retention": (
                    retained / len(observed) if observed else None
                ),
                "observed_grounded_retention_bootstrap_ci_low": boot_low,
                "observed_grounded_retention_bootstrap_ci_high": boot_high,
                "observed_grounded_retention_exact_ci_low": exact_low,
                "observed_grounded_retention_exact_ci_high": exact_high,
                "observed_ato_induced_loss": (
                    lost / len(observed) if observed else None
                ),
                "retention_worst_case_bound": (
                    retained / len(target) if target else None
                ),
                "retention_best_case_bound": (
                    (retained + missing) / len(target) if target else None
                ),
                "primary_inferential_scope": primary,
            }
        )
    return output


def _score_rows(
    merged: list[dict[str, Any]],
    episode_by_id: dict[str, dict[str, Any]],
    *,
    replicates: int,
    confidence: float,
    seed: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    scores: list[dict[str, Any]] = []
    bands: list[dict[str, Any]] = []
    sensitivities: list[dict[str, Any]] = []
    bundle_cache: dict[str, dict[str, Any]] = {}
    for scope, aou, model, rows, primary in _groups(merged):
        for dimension in DIMENSIONS:
            row_transitions = [
                (
                    row,
                    row["profile"]["diagnostic_score_transitions"][dimension],
                )
                for row in rows
            ]
            transitions = [transition for _, transition in row_transitions]
            c0 = [
                float(row["c0"]["score"])
                for row in transitions
                if isinstance(row["c0"].get("score"), (int, float))
            ]
            c1 = [
                float(row["c1"]["score"])
                for row in transitions
                if isinstance(row["c1"].get("score"), (int, float))
            ]
            paired = [
                (source, transition)
                for source, transition in row_transitions
                if isinstance(transition["c0"].get("score"), (int, float))
                and isinstance(transition["c1"].get("score"), (int, float))
            ]
            deltas = [
                float(transition["c1"]["score"])
                - float(transition["c0"]["score"])
                for _, transition in paired
            ]
            boot_low, boot_high = (
                _bootstrap_mean_ci(
                    deltas,
                    replicates=replicates,
                    confidence=confidence,
                    seed=_stable_seed(seed, "score", dimension, scope, aou, model or ""),
                )
                if primary
                else (None, None)
            )
            scores.append(
                {
                    "scope": scope,
                    "aou": aou,
                    "model": model,
                    "dimension": dimension,
                    "pair_n": len(rows),
                    "c0_numeric_n": len(c0),
                    "c1_numeric_n": len(c1),
                    "paired_numeric_n": len(paired),
                    "excluded_from_paired_numeric_n": len(rows) - len(paired),
                    "measurement_role": transitions[0]["measurement_role"],
                    "numeric_interpretation": transitions[0][
                        "numeric_interpretation"
                    ],
                    "c0_median": _quantile(c0, 0.5),
                    "c0_q1": _quantile(c0, 0.25),
                    "c0_q3": _quantile(c0, 0.75),
                    "c0_mean_descriptive": _mean(c0),
                    "c1_median": _quantile(c1, 0.5),
                    "c1_q1": _quantile(c1, 0.25),
                    "c1_q3": _quantile(c1, 0.75),
                    "c1_mean_descriptive": _mean(c1),
                    "paired_median_delta": _quantile(deltas, 0.5),
                    "paired_mean_delta": _mean(deltas),
                    "paired_mean_delta_bootstrap_ci_low": boot_low,
                    "paired_mean_delta_bootstrap_ci_high": boot_high,
                    "c1_lower_n": sum(value < 0 for value in deltas),
                    "unchanged_n": sum(value == 0 for value in deltas),
                    "c1_higher_n": sum(value > 0 for value in deltas),
                    "probability_c1_lower": (
                        sum(value < 0 for value in deltas) / len(deltas)
                        if deltas
                        else None
                    ),
                    "primary_inferential_scope": primary,
                }
            )
            def band_value(value: Any) -> str:
                return value if value in BANDS else "unavailable"

            band_counter = collections.Counter(
                (
                    band_value(row["c0"].get("band")),
                    band_value(row["c1"].get("band")),
                )
                for row in transitions
            )
            for c0_band in BAND_VALUES:
                for c1_band in BAND_VALUES:
                    bands.append(
                        {
                            "scope": scope,
                            "aou": aou,
                            "model": model,
                            "dimension": dimension,
                            "pair_n": len(rows),
                            "c0_band": c0_band,
                            "c1_band": c1_band,
                            "transition_n": band_counter[(c0_band, c1_band)],
                        }
                    )

            low_flags = []
            major_flags = []
            for source, _ in paired:
                membership = source["membership"]
                c0_episode = episode_by_id[membership["c0_episode_id"]]
                c1_episode = episode_by_id[membership["c1_episode_id"]]
                low_flags.append(
                    _episode_low_confidence(c0_episode, dimension, bundle_cache)
                    or _episode_low_confidence(c1_episode, dimension, bundle_cache)
                )
                c0_major = _episode_major_disagreement(c0_episode, dimension)
                c1_major = _episode_major_disagreement(c1_episode, dimension)
                if c0_major is True or c1_major is True:
                    major_flags.append(True)
                elif c0_major is False and c1_major is False:
                    major_flags.append(False)
                else:
                    major_flags.append(None)
            conservative_low = [
                float(transition["conservative_delta_range"]["low"])
                for _, transition in paired
            ]
            conservative_high = [
                float(transition["conservative_delta_range"]["high"])
                for _, transition in paired
            ]
            variants = (
                ("all_pairs_point", deltas),
                ("all_pairs_conservative_lower", conservative_low),
                ("all_pairs_conservative_upper", conservative_high),
                (
                    "exclude_low_confidence_pairs",
                    [value for value, flag in zip(deltas, low_flags) if not flag],
                ),
                (
                    "exclude_major_disagreement_pairs",
                    [
                        value
                        for value, flag in zip(deltas, major_flags)
                        if flag is False
                    ],
                ),
            )
            for variant, values in variants:
                variant_low, variant_high = (
                    _bootstrap_mean_ci(
                        values,
                        replicates=replicates,
                        confidence=confidence,
                        seed=_stable_seed(
                            seed,
                            "sensitivity",
                            dimension,
                            variant,
                            scope,
                            aou,
                            model or "",
                        ),
                    )
                    if primary and values
                    else (None, None)
                )
                sensitivities.append(
                    {
                        "scope": scope,
                        "aou": aou,
                        "model": model,
                        "dimension": dimension,
                        "sensitivity": variant,
                        "input_pair_n": len(rows),
                        "paired_numeric_n": len(paired),
                        "included_pair_n": len(values),
                        "excluded_non_numeric_pair_n": len(rows) - len(paired),
                        "excluded_by_sensitivity_n": len(paired) - len(values),
                        "excluded_major_disagreement_n": (
                            sum(flag is True for flag in major_flags)
                            if variant == "exclude_major_disagreement_pairs"
                            else None
                        ),
                        "excluded_disagreement_status_unavailable_n": (
                            sum(flag is None for flag in major_flags)
                            if variant == "exclude_major_disagreement_pairs"
                            else None
                        ),
                        "paired_mean_delta": _mean(values),
                        "paired_median_delta": _quantile(values, 0.5),
                        "paired_mean_delta_bootstrap_ci_low": variant_low,
                        "paired_mean_delta_bootstrap_ci_high": variant_high,
                        "strictly_negative_range_n": (
                            sum(value < 0 for value in conservative_high)
                            if variant == "all_pairs_point"
                            else None
                        ),
                        "strictly_positive_range_n": (
                            sum(value > 0 for value in conservative_low)
                            if variant == "all_pairs_point"
                            else None
                        ),
                        "range_overlaps_zero_n": (
                            sum(
                                low <= 0 <= high
                                for low, high in zip(
                                    conservative_low, conservative_high
                                )
                            )
                            if variant == "all_pairs_point"
                            else None
                        ),
                        "primary_inferential_scope": primary,
                    }
                )
    return scores, bands, sensitivities


def _descriptor_rows(merged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for scope, aou, model, rows, _ in _groups(merged):
        readiness = collections.Counter(
            (
                row["profile"]["stop_descriptor_transition"]["c0"][
                    "readiness_state"
                ],
                row["profile"]["stop_descriptor_transition"]["c1"][
                    "readiness_state"
                ],
            )
            for row in rows
        )
        stop_fit = collections.Counter(
            (
                row["profile"]["stop_descriptor_transition"]["c0"]["stop_fit"],
                row["profile"]["stop_descriptor_transition"]["c1"]["stop_fit"],
            )
            for row in rows
        )
        for kind, counter in (("readiness_state", readiness), ("stop_fit", stop_fit)):
            for (c0_value, c1_value), count in sorted(counter.items()):
                output.append(
                    {
                        "scope": scope,
                        "aou": aou,
                        "model": model,
                        "descriptor": kind,
                        "pair_n": len(rows),
                        "c0_value": c0_value,
                        "c1_value": c1_value,
                        "transition_n": count,
                        "descriptor_primary": True,
                    }
                )
    return output


def run(
    *,
    pair_profiles_path: Path,
    episode_states_path: Path,
    membership_path: Path,
    population_registry_path: Path,
    output_dir: Path,
    lock_path: Path,
    allow_real_data: bool,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    inputs = [
        pair_profiles_path,
        episode_states_path,
        membership_path,
        population_registry_path,
        lock_path,
    ]
    require_real_data_authorization(inputs, allow_real_data)
    lock = read_json_or_yaml(lock_path)
    registry = read_json_or_yaml(population_registry_path)
    if lock.get("freeze_status") != "frozen_authorized":
        raise GateError("statistics lock is not frozen_authorized")
    if lock.get("statistics_authorized") is not True:
        raise GateError("statistics are not authorized")
    if lock.get("paper_integration_authorized") is not False:
        raise GateError("statistics lock must keep paper integration disabled")
    if registry.get("freeze_status") != "frozen":
        raise GateError("statistics population registry is not frozen")
    if registry.get("effect_estimates_opened") is not False:
        raise GateError("statistics population registry already opened effects")

    pairs = load_jsonl(pair_profiles_path)
    episodes = load_jsonl(episode_states_path)
    memberships = load_jsonl(membership_path)
    pair_by_id = _unique(pairs, "pair_id", "pair")
    episode_by_id = _unique(episodes, "episode_id", "episode")
    membership_by_id = _unique(memberships, "pair_id", "membership")
    if set(pair_by_id) != set(membership_by_id):
        raise GateError("statistics pair and frozen membership sets differ")
    expected = lock["expected_structural_counts"]
    if len(pairs) != expected["pair_count"] or len(episodes) != expected["episode_count"]:
        raise GateError("statistics canonical input count drift")

    merged = []
    for pair_id in sorted(pair_by_id):
        profile = pair_by_id[pair_id]
        membership = membership_by_id[pair_id]
        if profile.get("aou") != membership.get("aou"):
            raise GateError(f"statistics AOU drift for {pair_id}")
        if profile.get("model") != membership.get("model"):
            raise GateError(f"statistics model drift for {pair_id}")
        if profile["verification_state_transition"]["c0_state"] != membership["c0_state"]:
            raise GateError(f"statistics C0 state drift for {pair_id}")
        if profile["verification_state_transition"]["c1_state"] != membership["c1_state"]:
            raise GateError(f"statistics C1 state drift for {pair_id}")
        if membership.get("effect_estimate_computed") is not False:
            raise GateError(f"statistics membership already opened effect: {pair_id}")
        merged.append({"membership": membership, "profile": profile, **membership})

    uncertainty = lock["uncertainty"]
    replicates = int(uncertainty["bootstrap_replicates"])
    confidence = float(uncertainty["confidence_level"])
    seed = int(uncertainty["bootstrap_seed"])
    plan = {
        "schema_version": "atobench.resilience_statistics_plan.v1",
        "status": "READY",
        "pair_count": len(merged),
        "bootstrap_replicates": replicates,
        "bootstrap_seed": seed,
        "confidence_level": confidence,
        "p_values_primary": False,
        "paper_integration_authorized": False,
    }
    if dry_run:
        return plan

    pooled, per_model = _state_distribution_rows(memberships)
    missingness = _missingness_rows(
        memberships,
        replicates=replicates,
        confidence=confidence,
        seed=seed,
    )
    transitions = _transition_rows(memberships)
    retention = _retention_rows(
        memberships,
        replicates=replicates,
        confidence=confidence,
        seed=seed,
    )
    scores, bands, judge_sensitivity = _score_rows(
        merged,
        episode_by_id,
        replicates=replicates,
        confidence=confidence,
        seed=seed,
    )
    descriptors = _descriptor_rows(merged)

    ensure_output_available(output_dir, new_version)
    paths = {
        "pooled": output_dir / "pooled_by_aou.csv",
        "per_model": output_dir / "per_model_by_aou.csv",
        "transitions": output_dir / "verification_state_transitions.csv",
        "capability": output_dir / "capability_conditioned.csv",
        "missingness": output_dir / "missingness_sensitivity.csv",
        "scores": output_dir / "diagnostic_score_statistics.csv",
        "bands": output_dir / "diagnostic_band_transitions.csv",
        "judge": output_dir / "judge_uncertainty_sensitivity.csv",
        "descriptors": output_dir / "stop_descriptor_transitions.csv",
        "tests": output_dir / "statistical_tests.json",
        "summary": output_dir / "statistics_summary.json",
    }
    state_fields = [
        "scope",
        "aou",
        "model",
        "condition",
        "pair_n",
        "state_observed_n",
        *[
            field
            for state in STATES
            for field in (f"{state}_n", f"{state}_proportion")
        ],
    ]
    write_csv(paths["pooled"], pooled, state_fields)
    write_csv(paths["per_model"], per_model, state_fields)
    write_csv(
        paths["transitions"],
        transitions,
        [
            "scope",
            "aou",
            "model",
            "complete_pair_n",
            "excluded_incomplete_pair_n",
            "c0_state",
            "c1_state",
            "transition_n",
            "transition_proportion_of_complete",
        ],
    )
    write_csv(paths["capability"], retention, list(retention[0]))
    write_csv(paths["missingness"], missingness, list(missingness[0]))
    write_csv(paths["scores"], scores, list(scores[0]))
    write_csv(paths["bands"], bands, list(bands[0]))
    write_csv(paths["judge"], judge_sensitivity, list(judge_sensitivity[0]))
    write_csv(paths["descriptors"], descriptors, list(descriptors[0]))

    inference = {
        "schema_version": "atobench.resilience_statistical_tests.v1",
        "p_values_computed": False,
        "confirmatory_test_family": None,
        "availability_exact_interval": "Clopper-Pearson interval for C1-observed share among availability-discordant pairs",
        "bootstrap": {
            "replicates": replicates,
            "seed": seed,
            "unit": "frozen_pair",
            "confidence_level": confidence,
            "percentile_interval": True,
            "missingness_pattern_retained": True,
        },
        "cross_aou_pooled_primary_estimate": False,
        "paper_facing_interpretation_performed": False,
    }
    write_json(paths["tests"], inference)
    summary = {
        "schema_version": "atobench.resilience_statistics_summary.v1",
        "status": "PASS",
        "created_at": utc_now(),
        "pair_count": len(merged),
        "aou_count": len({row["aou"] for row in merged}),
        "model_count": len({row["model"] for row in merged}),
        "complete_state_pair_count": sum(
            row["complete_state_pair"] for row in memberships
        ),
        "capability_target_pair_count": sum(
            row["c0_capability_target"] for row in memberships
        ),
        "capability_observed_outcome_pair_count": sum(
            row["c0_capability_outcome_observed"] for row in memberships
        ),
        "capability_missing_outcome_pair_count": sum(
            row["c0_capability_outcome_missing"] for row in memberships
        ),
        "bootstrap_replicates": replicates,
        "effect_estimates_opened": True,
        "statistics_performed": True,
        "p_values_computed": False,
        "paper_facing_analysis_performed": False,
        "paper_integration_authorized": False,
        "model_calls_made": 0,
        "network_accessed": False,
    }
    write_json(paths["summary"], summary)
    manifest = stage_manifest(
        "13_run_resilience_statistics",
        inputs,
        list(paths.values()),
        "complete",
        dry_run=False,
        details=summary,
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    return summary


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair-profiles", type=Path, required=True)
    parser.add_argument("--episode-states", type=Path, required=True)
    parser.add_argument("--population-membership", type=Path, required=True)
    parser.add_argument("--population-registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(
            pair_profiles_path=args.pair_profiles,
            episode_states_path=args.episode_states,
            membership_path=args.population_membership,
            population_registry_path=args.population_registry,
            output_dir=args.output,
            lock_path=args.lock,
            allow_real_data=args.allow_real_data,
            dry_run=args.dry_run,
            new_version=args.new_version,
        )
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0
