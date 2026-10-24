# Notes for coding agents
- Python 3.12, `a2a-sdk==1.2.3` (A2A spec 1.0). Many online examples target 0.x and are wrong.
  Use `a2a.helpers.new_data_part/new_data_message/get_data_parts`; there is no `DataPart` class.
- Contracts live in `common/envelope.py`. Change them only with agreement from the owners on both sides.
- Every agent: subclass `common.runtime.FreightAgent`, implement `handle()`, return a `ResultArtifact`.
- Delegation goes through `FreightAgent.delegate()` (enforces depth <= 3, no cycles, allowlist).
- State-changing actions call `common.policy.check()` first.
- Outreach must go through `common.outreach.route_target()`.
- Run `make lint test` before pushing.
