# Bounded Pipeline calls

PR Pipeline can add `--bounded-step` to the internal `pipeline` command. Repeat the same target, state path, pipeline run and position, model, policy, and `COPILOT_AGENT_SESSION_ID` until it returns a terminal result. Each call inspects checks once, dispatches or observes one hosted repair, or requests one CI rerun. `result: waiting` with exit code 0 means work remains pending. It is never CI clearance.

The hosted helper prints pending v5 JSON to stdout for direct CLI calls. Under managed execution, the coordinator reads pending JSON from the sealed child `workflow_result`. The helper keeps its checkpoint beside the result path and writes the final result file only after completion; the coordinator requires a matching sealed child result before reading that file. The standalone `run` command and `pipeline` without `--bounded-step` keep their synchronous behavior. An uncertain hosted dispatch stays bound to its original request. The coordinator does not send another request.

Both bounded and standalone coordinators poll the current-head check and workflow identities without downloading failed-job logs. Once the checks stabilize, they collect the failing logs once and recheck the live CI identities before dispatch. A changed check set restarts observation rather than sending stale logs to the worker.
