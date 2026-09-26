"""Local-first release automation pipeline.

Modules:
    models     - shared data types
    errors     - exception hierarchy
    versioning - reproducible version & changelog derivation
    cache      - content-addressed build cache
    builder    - multi-target build matrix
    deploy     - staged rollout, health gating, rollback
    state      - atomic, lock-protected persistent state
    validate   - request-contract validation (shared by run and plan)
    plan       - zero-trace dry-run planning
    record     - deterministic, comparable, verifiable release records
    pipeline   - end-to-end orchestration
    parity     - local vs CI parity checks
    cli        - command line entry point
"""

__version__ = "0.1.0"
