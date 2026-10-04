# Issue tracker: GitHub (+ local artifacts in docs/loophole/matt)

Issues and specs for this repo live as GitHub issues. Use the `gh` CLI for all operations.
File artifacts produced by the engineering skills (specs, ticket files, research notes)
live under `docs/loophole/matt/` — see "Local artifacts" below. All artifact text is in Russian.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

Infer the repo from `git remote -v`; `gh` does this automatically when run inside a clone.

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

When set to `yes`, PRs run through the same labels and states as issues, using the `gh pr` equivalents:

- **Read a PR**: `gh pr view <number> --comments` and `gh pr diff <number>` for the diff.
- **List external PRs for triage**: `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments` then keep only `authorAssociation` of `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE` (drop `OWNER`/`MEMBER`/`COLLABORATOR`).
- **Comment / label / close**: `gh pr comment`, `gh pr edit --add-label`/`--remove-label`, `gh pr close`.

GitHub shares one number space across issues and PRs, so a bare `#42` may be either: resolve with `gh pr view 42` and fall back to `gh issue view 42`.

## When a skill says "publish to the issue tracker"

- Durable issue (bug report, incoming request, spec, wayfinder map): create a GitHub issue.
- Working tickets for a feature being built in-session: create files under `docs/loophole/matt/<feature-slug>/issues/` (see below).

## When a skill says "fetch the relevant ticket"

GitHub ticket: `gh issue view <number> --comments`. Local ticket: read the file at the referenced path (the user normally passes the path or number directly).

## Local artifacts

Per-feature file artifacts live as markdown under `docs/loophole/matt/` (committed to the repo):

- One feature per directory: `docs/loophole/matt/<feature-slug>/`
- The spec is `docs/loophole/matt/<feature-slug>/spec.md`
- Implementation issues are one file per ticket at `docs/loophole/matt/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01`, never a single combined tickets file
- Blocking edges are declared as a `Blocked by: NN, NN` line near the top of a ticket; a ticket is unblocked when every file it lists is done
- Triage state is a `Status:` line near the top of each file (see `triage-labels.md` for the role strings); comments and conversation history append under a `## Comments` heading
- Repo-wide artifacts live at the top level: `docs/loophole/matt/GLOSSARY.md` and `docs/loophole/matt/adr/` (see `domain.md`)

Rule of thumb: anything that must be visible to others or needs tracker machinery (labels, dependencies, frontier queries) goes to GitHub; file artifacts (specs, tickets, research, retros) are saved under `docs/loophole/matt/` and committed.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single GitHub issue with **child** issues as tickets (fall back to `docs/loophole/matt/<effort>/map.md` + child files only when working without GitHub access).

- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `gh issue create --label wayfinder:map`.
- **Child ticket**: an issue linked to the map as a GitHub sub-issue (`gh api` on the sub-issues endpoint). Where sub-issues aren't enabled, add the child to a task list in the map body and put `Part of #<map>` at the top of the child body. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`). Once claimed, the ticket is assigned to the driving dev.
- **Blocking**: GitHub's **native issue dependencies**, the canonical, UI-visible representation. Add an edge with `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`, where `<blocker-db-id>` is the blocker's numeric **database id** (`gh api repos/<owner>/<repo>/issues/<n> --jq .id`, _not_ the `#number` or `node_id`). GitHub reports `issue_dependencies_summary.blocked_by` (open blockers only, the live gate). Where dependencies aren't available, fall back to a `Blocked by: #<n>, #<n>` line at the top of the child body. A ticket is unblocked when every blocker is closed.
- **Frontier query**: list the map's open children (`gh issue list --state open`, scoped to the map's sub-issues / task list), drop any with an open blocker (`issue_dependencies_summary.blocked_by > 0`, or an open issue in the `Blocked by` line) or an assignee; first in map order wins.
- **Claim**: `gh issue edit <n> --add-assignee @me`, the session's first write.
- **Resolve**: `gh issue comment <n> --body "<answer>"`, then `gh issue close <n>`, then append a context pointer (gist + link) to the map's Decisions-so-far.
