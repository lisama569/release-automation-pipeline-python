"""Local-first release automation pipeline.

Modules:
    models     - shared data types
    errors     - exception hierarchy
    versioning - reproducible version & changelog derivation
    cache      - content-addressed build cache
    builder    - multi-target build matrix
    deploy     - staged rollout, health gating, rollback
    state      - atomic, lock-protected persistent state
    pipeline   - end-to-end orchestration and exit codes
    parity     - local vs CI parity checks
    contract   - request-contract validation (reject-before-any-effect)
    planner    - pure rollout planning shared by preview and execution
    preview    - zero-trace machine-readable dry run
    record     - deterministic, comparable, self-verifying release records
    cli        - command line entry point
"""

__version__ = "0.1.0"
