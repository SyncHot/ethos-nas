---
name: todo-item
description: "Work a TODO.md item end to end. Use when fixing a P0/P1 or code-health item; re-verify anchors, implement minimally, run guards, flip the checkbox with the commit sha."
argument-hint: "TODO item ID (e.g. P1-04) or area"
---

# Work a TODO item

`TODO.md` is the single source of truth for health/quality work. Never create `docs/todo-*.md`.

## Procedure

### 1. Select correctly
- Read `TODO.md` → "How to use" and "Health scorecard".
- Priority order: **P0 before P1 before P2**… Never start an architecture item while a P0 is open.
- Do not bundle unrelated items.

### 2. Re-verify the item
The file is a snapshot; `path:line` anchors drift.
1. Open every cited file and locate the cited symbol (`grep_search`, not just the line number).
2. Reproduce the problem if it is runtime behavior.
3. If it is already fixed or wrong: **delete the item** and add a one-line note to the
   "Closed / not-an-issue" list. Do not leave a stale checkbox.

### 3. Plan the minimal change
- Stay inside the item's scope; no drive-by refactors.
- Respect the logging rule: child logger + every failure path logs.
- If the item touches routers/services, run the `security-invariant-review` skill afterwards.

### 4. Implement and verify
- Use the `verify-changes` skill.
- Add or extend a test that fails before the fix and passes after (see `add-test`).
- Do not restart the production service yourself; ask the user via Settings → Restart.

### 5. Close the item
- Flip `☐` → `☑` on the heading and append the commit sha:
  `### ☑ P1-04 · … — <sha>`
- If the item was partially done, keep it open and document exactly what remains (like P0-13 did).

### 6. Commit
Use `fix(<scope>): <what changed> (P0-xx)` / `feat(<scope>): …`, then push (AGENTS.md rules).

## Report
State the item ID, what was wrong, what changed, the verification commands/results, and whether
the checkbox was flipped. List changed files.

## Anti-patterns
- Implementing from the TODO text without verifying the code still looks like that.
- Marking done without running the guards or without a test.
- Mixing multiple TODO IDs in one commit.
- Creating a new backlog file.
