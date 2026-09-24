"""Transformer registry — maps primitive name -> transformer instance.

The addon imports REGISTRY and looks up the active primitive by name.
"""
from __future__ import annotations

from atobench.proxy.transformers.base import PrimitiveTransformer
from atobench.proxy.transformers.fake_version_banner import FakeVersionBannerTransformer
from atobench.proxy.transformers.vuln_symptom_inject import VulnSymptomInjectTransformer
from atobench.proxy.transformers.no_vuln_gaslighting import NoVulnGaslightingTransformer
from atobench.proxy.transformers.corrupt_belief import CorruptBeliefTransformer
from atobench.proxy.transformers.poison_dependency import PoisonDependencyTransformer
from atobench.proxy.transformers.false_completion import FalseCompletionTransformer
from atobench.proxy.transformers.exhaustion_trap import ExhaustionTrapTransformer
from atobench.proxy.transformers.induce_loop import InduceLoopTransformer
from atobench.proxy.transformers.substitute_subgoal import SubstituteSubgoalTransformer
from atobench.proxy.transformers.preemptive_ftp_gaslight import PreemptiveFtpGaslightTransformer
from atobench.proxy.transformers.cross_turn_jwt_escalation import CrossTurnJwtEscalationTransformer
from atobench.proxy.transformers.hardcoded_cred_comment import HardcodedCredCommentTransformer
from atobench.proxy.transformers.decoy_sql_search import DecoySqlSearchTransformer
from atobench.proxy.transformers.cross_turn_jwt_escalation_v2 import CrossTurnJwtEscalationV2Transformer
from atobench.proxy.transformers.decoy_sql_search_v2 import DecoySqlSearchV2Transformer

REGISTRY: dict[str, PrimitiveTransformer] = {
    t.NAME: t for t in [
        FakeVersionBannerTransformer(),
        VulnSymptomInjectTransformer(),
        NoVulnGaslightingTransformer(),
        CorruptBeliefTransformer(),
        PoisonDependencyTransformer(),
        FalseCompletionTransformer(),
        ExhaustionTrapTransformer(),
        InduceLoopTransformer(),
        SubstituteSubgoalTransformer(),
        PreemptiveFtpGaslightTransformer(),
        CrossTurnJwtEscalationTransformer(),
        HardcodedCredCommentTransformer(),
        DecoySqlSearchTransformer(),
        CrossTurnJwtEscalationV2Transformer(),
        DecoySqlSearchV2Transformer(),
    ]
}

__all__ = ["REGISTRY", "PrimitiveTransformer"]
