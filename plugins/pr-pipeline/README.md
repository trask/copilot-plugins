# PR Pipeline execution topology

Ask Copilot: **Show the PR Pipeline execution topology.**

```mermaid
flowchart TB
    request["Local PR Pipeline session<br/>starts once and watches progress"]
    scheduler["Detached scheduler<br/>owns durable control flow"]
    reviewer["PR Reviewer<br/>standalone agent<br/>not a pipeline stage"]

    request --> scheduler
    request -. "separate invocation" .-> reviewer

    subgraph local["Local coordinators"]
        direction LR
        conflict["1. pr-conflict-resolver"]
        copilotReview["2. copilot-review-loop"]
        selfReview["3. self-review-loop"]
        ciFix["4. ci-fix-loop"]
        description["5. pr-description"]
        clearance["Final head and base clearance"]

        conflict --> copilotReview --> selfReview --> ciFix --> description --> clearance
        reviewWorker["Local Copilot decision session<br/>gpt-5.6-sol, high<br/>marketplace-local-review-decision-worker@2<br/>no hosted fallback"]
        copilotReview -. "one decision worker per fixing iteration" .-> reviewWorker
    end

    scheduler --> conflict

    subgraph hosted["GitHub Agent Task REST API and hosted worker sessions"]
        conflictWorker["Conflict worker<br/>marketplace-conflict-worker@1"]
        selfReviewWorker["Self-review worker<br/>marketplace-agent-apply-report-worker@3"]
        ciWorker["CI worker<br/>marketplace-agent-apply-report-worker@3"]
        descriptionWorker["Description worker<br/>marketplace-agent-report-worker@1"]
    end

    conflict -. "local Agent Tasks Runtime dispatches and verifies" .-> conflictWorker
    selfReview -. "local Agent Tasks Runtime dispatches and verifies" .-> selfReviewWorker
    ciFix -. "local Agent Tasks Runtime dispatches and verifies" .-> ciWorker
    description -. "local Agent Tasks Runtime dispatches and verifies" .-> descriptionWorker

    clearance -->|"all five stages clear at one revision snapshot"| done["Pipeline complete"]
    clearance -->|"head or base revision drift leaves a stage uncleared"| second["Second sweep<br/>same stage order<br/>skip a completed conflict resolver"]
    second --> conflict
```

The scheduler runs the five stages in the numbered order. It starts a second sweep only when the pull request head or base changes during the first sweep and a stage is not clear at the final revisions. A completed conflict resolver does not run again in that pipeline run.

Each stage has a local coordinator. For hosted stages, the local Agent Tasks Runtime dispatches the request and verifies the result. It is not a worker session. Copilot Review is different. Its coordinator starts a local `gpt-5.6-sol` session with reasoning effort `high` under `marketplace-local-review-decision-worker@2`, and it has no hosted fallback.

| Stage | Worker session count when work is needed |
| --- | --- |
| PR Conflict Resolver | One hosted Agent Task session per conflict attempt |
| Copilot Review | One local decision session per fixing iteration |
| Self Review | One hosted Agent Task session |
| CI Fix | One hosted Agent Task session per distinct stable CI failing snapshot |
| PR Description | One hosted Agent Task session |

The CI coordinator owns polling, reruns, stabilization, and deduplication. Repeated observations of the same stable CI failing snapshot do not start another hosted session. A changed stable snapshot may start the next one.

PR Reviewer is a standalone agent. PR Pipeline does not call it as a stage or worker.
