# PR Pipeline execution topology

Ask Copilot: **Show the PR Pipeline execution topology.**

```mermaid
flowchart LR
    pipelineAgent["PR Pipeline agent<br/>LOCAL SESSION"]
    scheduler["PR Pipeline scheduler<br/>LOCAL PROCESS<br/>detached"]
    pipelineAgent --> scheduler

    subgraph pipeline["PR Pipeline, five-stage boundary"]
        direction TB

        subgraph sweep["Ordered Pipeline sweep"]
            direction TB

            subgraph local["LOCAL coordinators and local worker"]
                direction LR
                conflict["1. PR Conflict Resolver<br/>local coordinator"]
                copilotReview["2. Copilot Review Loop<br/>local coordinator"]
                selfReview["3. Self Review Loop<br/>local coordinator"]
                ciFix["4. CI Fix Loop<br/>local coordinator"]
                description["5. PR Description<br/>local coordinator"]
                reviewWorker["Copilot Review worker<br/>1x local decision session / fixing iteration<br/>ALL findings in the stable review snapshot<br/>gpt-5.6-sol, high<br/>marketplace-local-review-decision-worker@2<br/>no hosted fallback"]

                conflict --> copilotReview --> selfReview --> ciFix --> description
                copilotReview -. "paired local worker" .-> reviewWorker
            end

            subgraph hosted["HOSTED Agent Task workers"]
                direction LR
                conflictWorker["Conflict worker<br/>1x / managed attempt<br/>ALL frozen conflict paths and selected stack members<br/>marketplace-conflict-worker@1"]
                selfReviewWorker["Self Review worker<br/>1x / review iteration<br/>ALL self-review findings<br/>marketplace-agent-apply-report-worker@3"]
                ciWorker["CI Fix worker<br/>1 HOSTED agent receives ALL failures at the current PR head<br/>A new agent starts only after the head or final check results change<br/>NOT one agent per failed check<br/>marketplace-agent-apply-report-worker@3"]
                descriptionWorker["PR Description worker<br/>1x report task / whole PR<br/>title and body decision<br/>marketplace-agent-report-worker@1"]
            end

            conflict -. "paired worker via local dispatcher/verifier" .-> conflictWorker
            selfReview -. "paired worker via local dispatcher/verifier" .-> selfReviewWorker
            ciFix -. "paired worker via local dispatcher/verifier" .-> ciWorker
            description -. "paired worker via local dispatcher/verifier" .-> descriptionWorker
        end

        revision{"Head or base changed<br/>and left a stage uncleared?"}
        description --> revision
        revision -->|"no"| done["Pipeline complete"]
        revision -->|"yes"| second["Optional second sweep<br/>may create new workers for uncleared changed revisions"]
        second --> conflictComplete{"Conflict Resolver completed?"}
        conflictComplete -->|"no"| conflict
        conflictComplete -->|"yes, never relaunch it"| copilotReview
    end

    subgraph reviewer["Standalone PR Reviewer, not a pipeline stage"]
        direction TB
        reviewerCoordinator["PR Reviewer coordinator<br/>LOCAL"]
        reviewerWorker["Whole-PR report worker<br/>1x HOSTED report task<br/>marketplace-agent-report-worker@1"]
        candidates["Coordinator verifies the report<br/>and extracts N candidates"]
        evaluators["N fresh LOCAL evaluator sessions<br/>1x general-purpose session / candidate<br/>gpt-5.6-sol, max<br/>evidence-only, no checkout<br/>at most 1x replacement if that verdict is malformed"]
        pendingReview["1x viewer-owned pending review<br/>ALL surviving findings<br/>never submitted"]

        reviewerCoordinator --> reviewerWorker
        reviewerWorker -->|"one authoritative report"| candidates
        candidates -->|"N candidates"| evaluators
        evaluators -->|"surviving findings"| pendingReview
    end

    scheduler --> conflict
    legend["Cardinality legend<br/>1x/attempt, 1x/iteration, 1x/current-head CI batch, Nx/candidate"]
```

The scheduler runs the five stages in the numbered order. It starts a second sweep only when the pull request head or base changes during the first sweep and a stage is not clear at the final revisions. A completed conflict resolver does not run again in that pipeline run.

Each stage has a local coordinator. For hosted stages, the local Agent Tasks Runtime dispatches the request and verifies the result. It is not a worker session. Copilot Review is different. Its coordinator starts a local `gpt-5.6-sol` session with reasoning effort `high` under `marketplace-local-review-decision-worker@2`, and it has no hosted fallback.

| Stage | Worker cardinality and bundle |
| --- | --- |
| PR Conflict Resolver | One hosted Agent Task per managed attempt. It receives every frozen conflict path and all selected stack members in that attempt. |
| Copilot Review | One local decision session per fixing iteration. It receives all findings in the stable review snapshot. |
| Self Review | One hosted Agent Task per review iteration. It reviews and handles all self-review findings in that iteration. |
| CI Fix | One hosted Agent Task receives all failures and logs at the current pull request head. A new task starts only after the head or final check results change. It is not one task per failed check. |
| PR Description | One hosted report task for the whole pull request title and body decision. |

The CI coordinator owns polling, reruns, stabilization, and snapshot deduplication. Stable means all relevant checks are terminal and the check rollup remains unchanged after debounce. Repeated observations of the same current-head stable final check set do not start another hosted session. It does not deduplicate log content across checks or matrix jobs.

For every failing check, the current coordinator runs `gh run view <run> [--job <job>] --log-failed --allow-escape-sequences`. It stores the complete returned standard output as `failures[].log` with its SHA-256 digest, then JSON-serializes the entire check snapshot into the hosted worker prompt. It does not pass successful-job logs, and `--log-failed` usually limits output to failed steps.

No log compaction exists today. The coordinator does not deduplicate or normalize logs across matrix jobs, truncate them, extract excerpts, or enforce an aggregate prompt byte or token budget. One hundred similar failing matrix jobs can therefore duplicate substantial text in one worker prompt. This is a known scalability gap.

PR Reviewer is a standalone workflow that runs only when invoked directly. PR Pipeline does not call it as a stage or worker. Its local coordinator dispatches one hosted `marketplace-agent-report-worker@1` task for the whole-pull-request authoritative report, verifies the result, and extracts the candidate set against the authoritative diff.

Each candidate goes to one fresh local `general-purpose` evaluator using `gpt-5.6-sol` with reasoning effort `max`. Evaluators are evidence-only. They receive the candidate evidence and relevant diff excerpt, but no checkout. They cannot fetch, inspect, or change the source. A malformed evaluator verdict permits at most one fresh replacement for that candidate. If findings survive evaluation, the coordinator creates one viewer-owned pending review containing all survivors and never submits it.
