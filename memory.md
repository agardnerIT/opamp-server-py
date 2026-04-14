# Decisions

## Commit/Issue Protocol

- Always create a GitHub issue BEFORE implementing a new feature
- Never write sensitive information to Github issues (or anywhere else public for that matter)
- Link issues to commits and / or PRs (use "Closes #XX" or "Refs #XX" in commit message)
- Never close issues until the user confirms they're resolved

## Environment Variables

- All new configurable settings should be added to `.env.sample`

## Planning Process

- When planning an issue, post the plan to the GitHub issue BEFORE modifying any code
- Wait for human review/approval before actioning

## Issue Closing

- When closing an issue: use "Closes #XX" in commit message
- ALWAYS push commits after closing so the fix shows in the issue
- User must be able to see which commit/PR closed the issue