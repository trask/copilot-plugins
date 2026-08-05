When working on an existing pull request, push commits to that pull request's existing head (source) repository and branch. When a local branch already has an upstream/tracking branch, push to that existing branch. Only when there is no existing pull request head or upstream/tracking branch, push the branch to my fork instead of directly to the upstream repository, and open the pull request from my fork into the upstream repository.

When opening a pull request, always create it as a draft and always request a Copilot review.

When reviewing a pull request, name the GitHub Copilot session `Review: <PR title>`. Do not apply this naming rule to other pull request work.

Never run `spotlessCheck` as a preliminary check before running `spotlessApply`. Run `spotlessApply` directly because it takes the same amount of time and fixes formatting issues.
