# Pipeline stage calls

PR Pipeline may call `scripts/self_review_loop.py pipeline` with `--bounded-step`, a 32-character lowercase hex `--pipeline-run`, an external `--state` path, and its usual target, iteration, model, and policy arguments. Repeat the same command from the same `COPILOT_AGENT_SESSION_ID` until it returns a terminal result. Each call runs in the foreground with an 85-second stage deadline, leaving room under Pipeline's 90-second step limit.

`{"result":"waiting"}` means the stage is not finished. It does not clear review. Each hosted task performs one pass. The first call dispatches a task; later calls observe it and reuse its pinned input and artifacts. After the task completes, the stage verifies the candidate and publishes its source changes. Verified code commits count as one pass even without a structured outcome file; the next call starts a new task on the published head. A pass without code requires an explicit clean outcome to clear review. Publication visibility may require more waiting calls. Changing the session, run, state, target, iteration, model, policy, or budget cannot adopt an unfinished task.

The completed observation also returns waiting. The next call verifies the result and begins publication with a fresh execution deadline. Completed code passes return waiting until a later clean pass or the iteration budget is exhausted.

A later sweep in the same run starts from a fresh PR preflight. The target PR and completed task history remain bound, but the current branch, head, base, draft state, title, and body need not match the prior sweep. The stage keeps its spent iteration count; it only accepts a new hosted result against that task's pinned inputs.

The cloud helper reports pending status as JSON on stdout and stores its checkpoint at `<result-file>.pipeline.json`. The final v5 result file does not exist until task completion. After successful publication, the stage removes the checkpoint with the other task artifacts unless `--preserve-artifacts` is set.

Without `--bounded-step`, pipeline and standalone commands retain their existing synchronous behavior.
