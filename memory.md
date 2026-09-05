# Decisions

## Design Principles

- As simple as possible. As complex as necessary.

## Commit/Issue Protocol

- "Use 'bd' for task tracking" and open an issue on GitHub
- When a task is done, close it using 'bd' and write a summary to the Git issue then close it out.
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

# Technical Discoveries

- `st.page_link()` throws `KeyError: 'url_pathname'` with string paths or improperly resolved paths in this project structure
- `str.format()` conflicts with JSON braces in alert body templates; use `str.replace()` instead
- Streamlit multi-page apps require `st.set_page_config()` in each page file
- Duplicate `st.form()` keys cause `StreamlitAPIException`; use unique keys per page/context
- `st.rerun()` immediately after `st.error()` clears the error; use session state flags or `st.stop()` to persist errors
- Test alert button should autosave config before sending, otherwise unsaved templates are ignored
- Generated protobuf code (`proto/opamp_pb2.py`) requires runtime >= the protoc gencode version; if tests die at collection with `google.protobuf.runtime_version.VersionError`, upgrade: `pip install -U 'protobuf>=7.34.1'` (7.36.1 on this machine)
- Manifest generation lives in `server/manifest.py` (moved from `ui/manifest.py`) — shared by `POST /agent/{id}/manifest` and the UI; user-supplied versions must go through `validate_manifest_version()`
- `GET /agents` supports query filtering (matching logic in `AgentState.matches_filters()`/`get_attr()` in `server/state.py`): `healthy=true|false|unknown` (Literal → 422 on bad values), `status=`/`remote_config_status=` (remote config, case-insensitive, reserved names win over metadata), and any other param filters description attributes (identifying + non-identifying, stringified AnyValue, case-insensitive, repeated params = OR); response includes a `filters` echo