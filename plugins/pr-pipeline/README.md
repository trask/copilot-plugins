# PR Pipeline execution topology

Ask Copilot: **Show the PR Pipeline execution topology.**

```mermaid
flowchart LR
    subgraph pipeline["PR Pipeline, five-stage boundary"]
        direction TB
        request["Local PR Pipeline session<br/>starts once and watches progress"]
        scheduler["Detached scheduler<br/>owns durable control flow"]
        request --> scheduler

        subgraph local["Ordered local stage coordinators"]
            direction LR
            conflict["1. pr-conflict-resolver"]
            copilotReview["2. copilot-review-loop"]
            selfReview["3. self-review-loop"]
            ciFix["4. ci-fix-loop"]
            description["5. pr-description"]
            clearance["Final head and base clearance"]

            conflict --> copilotReview --> selfReview --> ciFix --> description --> clearance
            reviewWorker["One local decision session per iteration<br/>all snapshot findings<br/>gpt-5.6-sol, high<br/>marketplace-local-review-decision-worker@2<br/>no hosted fallback"]
            copilotReview -. "local worker boundary" .-> reviewWorker
        end

        scheduler --> conflict

        subgraph hosted["GitHub Agent Task REST API and hosted worker sessions"]
            conflictWorker["One conflict task per attempt<br/>all conflicts and stack members<br/>marketplace-conflict-worker@1"]
            selfReviewWorker["One self-review task per iteration<br/>all findings<br/>marketplace-agent-apply-report-worker@3"]
            ciWorker["One CI task per current-head<br/>stable final check set<br/>all failures<br/>marketplace-agent-apply-report-worker@3"]
            descriptionWorker["One description task per whole PR<br/>marketplace-agent-report-worker@1"]
        end

        conflict -. "local Agent Tasks Runtime dispatches and verifies" .-> conflictWorker
        selfReview -. "local Agent Tasks Runtime dispatches and verifies" .-> selfReviewWorker
        ciFix -. "local Agent Tasks Runtime dispatches and verifies" .-> ciWorker
        description -. "local Agent Tasks Runtime dispatches and verifies" .-> descriptionWorker

        clearance -->|"all five stages clear at one revision snapshot"| done["Pipeline complete"]
        clearance -->|"head or base revision drift leaves a stage uncleared"| second["Second sweep<br/>same stage order<br/>skip a completed conflict resolver"]
        second --> conflict
    end

    subgraph reviewer["Standalone PR Reviewer, not a pipeline stage"]
        direction TB
        reviewerCoordinator["Local PR Reviewer coordinator"]
        reviewerWorker["One hosted whole-PR report task<br/>marketplace-agent-report-worker@1"]
        candidates["Coordinator verifies the report<br/>and extracts N candidates"]
        evaluators["Fan-out of N fresh local sessions<br/>one general-purpose evaluator per candidate<br/>gpt-5.6-sol, max<br/>evidence-only, no checkout"]
        pendingReview["One viewer-owned pending review<br/>contains all surviving findings<br/>never submitted"]

        reviewerCoordinator --> reviewerWorker
        reviewerWorker -->|"authoritative report"| candidates
        candidates --> evaluators
        evaluators -->|"surviving findings"| pendingReview
    end
```

The scheduler runs the five stages in the numbered order. It starts a second sweep only when the pull request head or base changes during the first sweep and a stage is not clear at the final revisions. A completed conflict resolver does not run again in that pipeline run.

Each stage has a local coordinator. For hosted stages, the local Agent Tasks Runtime dispatches the request and verifies the result. It is not a worker session. Copilot Review is different. Its coordinator starts a local `gpt-5.6-sol` session with reasoning effort `high` under `marketplace-local-review-decision-worker@2`, and it has no hosted fallback.

| Stage | Worker cardinality and bundle |
| --- | --- |
| PR Conflict Resolver | One hosted Agent Task per attempt. It receives all conflicts and every stack member in that attempt. |
| Copilot Review | One local decision session per iteration. It receives all findings in the frozen review snapshot. |
| Self Review | One hosted Agent Task per iteration. It reviews and handles all findings in that iteration. |
| CI Fix | One hosted Agent Task per current-head stable final check set. It receives all failures in that set. A new task starts only after the head or final check results change. |
| PR Description | One hosted Agent Task for the whole pull request. |

The CI coordinator owns polling, reruns, stabilization, and snapshot deduplication. Repeated observations of the same current-head stable final check set do not start another hosted session. It does not deduplicate log content across checks or matrix jobs.

For every failing check, the current coordinator runs `gh run view <run-id> [--job <job-id>] --log-failed --allow-escape-sequences`. It embeds the complete returned standard output for that check and its SHA-256 digest in the worker prompt. There is no cross-matrix log deduplication, truncation, excerpting, or aggregate prompt budget.

PR Reviewer is a standalone workflow that runs only when invoked directly. PR Pipeline does not call it as a stage or worker. Its local coordinator dispatches one hosted `marketplace-agent-report-worker@1` task for the whole-pull-request authoritative report, verifies the result, and extracts the candidate set against the authoritative diff.

Each candidate goes to one fresh local `general-purpose` evaluator using `gpt-5.6-sol` with reasoning effort `max`. Evaluators are evidence-only. They receive the candidate evidence and relevant diff excerpt, but no checkout. They cannot fetch, inspect, or change the source. If findings survive evaluation, the coordinator creates one viewer-owned pending review containing all survivors and never submits it.
