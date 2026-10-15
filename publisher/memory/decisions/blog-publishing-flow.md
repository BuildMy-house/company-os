# Decision: posts publish by PR, never by push to main

Context: the blog repo deploys from main. Decision: the publisher pushes only blog/<slug>
(or reports/<slug>) and opens a PR; merging is a human or Board-approved action.
Why: a bad post is public instantly and hard to retract; review is cheap.
Enforcement: git wrapper plus pre-push hook (defense in depth); the real control is a
one-repo fine-grained token and branch protection on main.
