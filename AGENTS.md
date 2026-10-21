# Agent instructions

## Local-first development and testing

- Perform new model/task development, backtests, experiments, and repeated tests in the user's authorized local environment by default. Do not use GitHub Actions as an iterative test runner.
- Before pushing or opening/updating a pull request, inspect workflow triggers and path filters to avoid unintended expensive CI. Use a supported documentation-only CI skip marker where appropriate; never bypass branch protections or required safety checks.
- Publish stable code and retain only necessary production automation. This convention does not authorize disabling existing safety checks or altering production schedules.
- If local access is unavailable, report the blocker rather than silently switching tests to paid/private GitHub Actions.
- These instructions are repository-scoped guidance, not a technically enforced account-wide setting. When creating a new model/task repository, carry this convention into its `AGENTS.md`.
