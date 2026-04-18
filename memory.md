# Decisions

## Design Principles

- As simple as possible. As complex as necessary.

## Commit/Issue Protocol

- Always create a GitHub issue BEFORE implementing a new feature
- Never write sensitive information to Github issues
- Link issues to commits (use "Closes #XX" or "Refs #XX" in commit message)
- NEVER close issues until the user confirms they're resolved

## Issue Handling

- When asked to do an issue, FIRST look it up and understand it
- Fix the issue and add/update comments on the issue
- Link commits to the issue but DO NOT close until explicitly told
- This applies to all future issue work

## Scope Changes

- If scope changes (because user wasn't clear enough in issue), update the original issue/comment to explain the new scope and why you're doing more work

## Environment Variables

- All new configurable settings should be added to `.env.sample`

## Planning Process

- When planning an issue, post the plan to the GitHub issue BEFORE modifying any code
- Wait for human review/approval before actioning
- You can always create subtasks if a task is too big / if it's easier

## Issue Closing

- ALWAYS push commits after closing so the fix shows in the issue
- User must be able to see which commit/PR closed the issue

## Additional Ideas

- ALWAYS ask for clarification if needed
- ALWAYS prompt user with ideas they may not have thought of

## Commits

- NEVER commit to git until user confirms issue is resolved
- NEVER push without explicit user approval

## UI Changes

- Whenever there are UI changes, take screenshots and add them to the README so users always see the latest and greatest UI
- Before taking screenshots: start a collector (`~/tools/otelcol-contrib --config=collector/config.yaml`). Wait 5 seconds then start the UI. Disregard the initial collector error logs. That's expected as the collector cannot connect.

## Dependencies

- Always keep dependencies up to date with latest versions
- Check for updates regularly and update requirements files accordingly