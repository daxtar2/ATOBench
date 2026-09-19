"""ATOBench: Adversarial Deception against Long-Horizon LLM pentest agents.

A black-box evaluation framework for environment-side deception against
long-horizon LLM pentest agents. Provides:

- Target profiles and run manifests for reproducible episode configuration
- BaseAgent ABC for plugging in third-party pentest agents
- Coupling-mode axis (loose / schema_coupled / precondition / signal_removal)
  as the primary deception-strength taxonomy
- RuntimeProgram-based MITM perturbations with runtime event attribution
- Clean-relative pentest-effect evaluation for paired target runs
- Report normalization for black-box pentest agent outputs
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
