# Decision: posts publish by PR, never by push to main

Context: the blog repo deploys from main. Decision: the publisher pushes only blog/<slug>
(on website, hermees-diary or observer-website only) and opens a PR; merging is a human or Board-approved action.
Why: a bad post is public instantly and hard to retract; review is cheap.
Enforcement: git wrapper plus pre-push hook (defense in depth); the real control is a
fine-grained token limited to those three repos and branch protection on main.
