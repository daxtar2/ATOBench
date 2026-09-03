"""Compatibility build entry point for environments without PEP 621 support."""

from setuptools import find_packages, setup


PACKAGE_DATA = [
    "schema/*.json",
    "scripts/*",
    "experiment/AOM_REGISTRY.yaml",
    "experiment/ground_truth/*.yaml",
    "examples/experiments/protocol_v3/*.yaml",
    "experiment/protocol_v3/**/*.yaml",
    "experiment/protocol_v3/**/*.json",
    "experiment/protocol_v3/**/*.md",
    "targets/juice-shop/*.yml",
    "targets/juice-shop/**/*.yaml",
    "targets/juice-shop/**/*.yml",
    "targets/juice-shop/**/*.json",
    "targets/juice-shop/**/*.jsonl",
]


setup(
    name="atobench-cross-model",
    version="0.1.0",
    description="Reproducible runtime for ATOBench paired cross-model experiments",
    python_requires=">=3.10",
    packages=find_packages(),
    include_package_data=True,
    package_data={"atobench": PACKAGE_DATA},
    exclude_package_data={
        "atobench": [
            "targets/juice-shop/experiments/**",
            "**/__pycache__/**",
            "**/*.pyc",
            "**/.DS_Store",
        ]
    },
    install_requires=["PyYAML>=6.0", "jsonschema>=4.0", "mitmproxy>=10.0"],
    extras_require={"dev": ["pytest>=8.0"]},
    entry_points={
        "console_scripts": [
            "atobench-cross-model=atobench.experiment.cross_model_protocol:main",
            "atobench-experiment=atobench.cli.main:main",
        ]
    },
)
