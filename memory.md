# Decisions

## Design Principles

- As simple as possible. As complex as necessary.

## Commit/Issue Protocol

- Always create a GitHub issue BEFORE implementing a new feature
- Never write sensitive information to Github issues
- Link issues to commits (use "Closes #XX" or "Refs #XX" in commit message)
- NEVER close issues until the user confirms they're resolved
- Always add a comment to issues and comments that this work was assisted by AI
- Compact your memory after we close each issue

## Issue Handling

- When asked to do an issue, FIRST look it up and understand it
- Create as mant subtasks as you need to simplify work
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

# Completed Work

## Issue #42: Admin Mode Consolidation
- Consolidated scattered admin operations into a single Admin page/tab with unified password prompt
- Fixed duplicate form keys, wrong password UX, and broken agent navigation links
- Committed & closed

## Issue #43: Configurable CloudEvents Alerts
- Implemented configurable alert headers/body templates with CloudEvents 1.0 defaults
- Added autosave before test alert, removed global `ALERT_ENABLED` toggle
- Fixed JSON brace substitution using `str.replace()` instead of `str.format()`
- Added pretty-printed JSON defaults
- Committed & closed

## Issue #44: Streamlit Multi-Page UI (In Progress)
- Restructured UI from tab-based (`st.tabs()`) to Streamlit's native multi-page architecture
- Created `ui/shared.py` for shared imports, constants, sidebar navigation
- Created pages: `1_Agents.py`, `2_Reports.py`, `3_Admin.py`, `4_Help.py`
- Replaced `st.page_link()` (crashes with `KeyError: 'url_pathname'`) with `st.button()` + `st.switch_page(Path(...))`
- Maintained `?agent_id=` query parameter routing for agent detail views

# Technical Discoveries

- `st.page_link()` throws `KeyError: 'url_pathname'` with string paths or improperly resolved paths in this project structure
- `str.format()` conflicts with JSON braces in alert body templates; use `str.replace()` instead
- Streamlit multi-page apps require `st.set_page_config()` in each page file
- Duplicate `st.form()` keys cause `StreamlitAPIException`; use unique keys per page/context
- `st.rerun()` immediately after `st.error()` clears the error; use session state flags or `st.stop()` to persist errors
- Test alert button should autosave config before sending, otherwise unsaved templates are ignored