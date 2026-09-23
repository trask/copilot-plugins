# Pipeline stage calls

PR Pipeline may call `scripts/self_review_loop.py pipeline` with `--bounded-step`, a 32-character lowercase hex `--pipeline-run`, an external `--state` path, and its usual target, iteration, model, and policy arguments. Repeat the same command from the same `COPILOT_AGENT_SESSION_ID` until it returns a terminal result. Each call runs in the foreground with an 85-second stage deadline, leaving room under Pipeline's 90-second step limit.

`{"result":"waiting"}` means the stage is not finished. It does not clear review. The first call dispatches one hosted task; later calls observe that task and reuse its pinned input and artifacts. After the task completes, the stage verifies the candidate and publishes its source changes. Publication visibility may require more waiting calls. Changing the session, run, state, target, iteration, model, policy, or budget cannot adopt the unfinished task.

The completed observation also returns waiting. The next call verifies the result and begins publication with a fresh execution deadline.

The cloud helper reports pending status as JSON on stdout and stores its checkpoint at `<result-file>.pipeline.json`. The final v5 result file does not exist until task completion. After successful publication, the stage removes the checkpoint with the other task artifacts unless `--preserve-artifacts` is set.

Without `--bounded-step`, pipeline and standalone commands retain their existing synchronous behavior.
