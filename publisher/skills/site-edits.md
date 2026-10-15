---
name: site-edits
description: How to change the three site repos safely
---
# Editing the site repos (website, hermees-diary, observer-website)

1. `publisher repo list` shows each repo, its verify commands and what is known about its deploy.
2. `publisher repo sync <name>` clones or fast-forwards to origin/main (refuses a dirty tree).
3. `publisher repo branch <name> blog/<slug>` branches off origin/main. Only blog/<slug> can be pushed.
4. Edit files under /workspace/<name>. Never put credentials, emails or subscriber data in a repo.
5. `publisher repo verify <name>` runs the repo's own CI (lint, check, build). Fix failures first.
6. `publisher repo pr <name> --branch blog/<slug> --message "..."` commits, pushes, opens the PR.
7. Blog posts: use `publisher blog pr` (hermees-diary), not this flow.
8. Do not merge. Merging is the human decision and may publish the site; say in the hand-back
   what is unverified (no live deploy path is confirmed for any of the three).
