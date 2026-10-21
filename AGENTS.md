# Agent instructions

## Cloud-first development and testing

- Perform new research, model/task development, backtests, experiments, and improvement tests in dot's cloud execution environment by default, not GitHub Actions. Do not use GitHub Actions as an iterative test runner.
- If cloud access or environment limitations block the task, use an authorized user-local environment only within granted permissions. Never bypass an access denial; report any remaining blocker rather than silently switching tests to paid/private GitHub Actions.
- Stable production tasks that need recurring automation may use GitHub Actions. This convention does not authorize disabling existing safety checks or altering production schedules.
- Private-repository GitHub-hosted jobs consume included quota or incur charges regardless of whether they are called tests or production, including push/PR checks. Public repositories' standard GitHub-hosted runners are generally free; do not assume larger runners or every public-repository configuration are free.
- Before pushing or opening/updating a pull request, inspect workflow triggers and path filters to avoid unintended expensive CI. Use a supported documentation-only CI skip marker where appropriate; never bypass branch protections or required safety checks.
- These instructions are repository-scoped guidance, not a technically enforced account-wide setting. When creating a new model/task repository, carry this convention into its `AGENTS.md`.
