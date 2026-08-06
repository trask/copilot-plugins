When working on an existing pull request, push commits to that pull request's existing head (source) repository and branch. When a local branch already has an upstream/tracking branch, push to that existing branch. Only when there is no existing pull request head or upstream/tracking branch, push the branch to my fork instead of directly to the upstream repository, and open the pull request from my fork into the upstream repository.

When opening a pull request, always create it as a draft and always request a Copilot review.

When reviewing a pull request, name the GitHub Copilot session `Review: <PR title>`. Do not apply this naming rule to other pull request work.

Never create GitHub issues without my explicit approval, and treat this as overriding autopilot mode's instruction to decide rather than ask. Propose the issue and wait. When something is worth recording but out of scope for the current change, put it in the pull request description or a pull request comment instead of filing an issue.

Never run `spotlessCheck` as a preliminary check before running `spotlessApply`. Run `spotlessApply` directly because it takes the same amount of time and fixes formatting issues.

Never hard wrap GitHub pull request and issue descriptions or comments. Let each paragraph be a single long line and let GitHub wrap it.
