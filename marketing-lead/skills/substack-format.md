---
name: substack-format
description: Writing Substack drafts through the marketing-lead tools
---
# Substack format

- Substack is reached through the sidecar (unofficial, session-cookie based; it can break or
  expire without notice). If a call fails with auth_failed, report it and stop.
- create_draft_post takes plain text: one paragraph per line. Markdown is NOT interpreted, so
  write no #, **, or [text](url) syntax; put URLs on their own line.
- Title under 70 chars, subtitle one sentence. Newsletter posts are shorter than blog posts.
- Draft: marketing-lead substack draft --title T --subtitle S --body B
- Publish needs approval_ref and goes live on the web only; no email is ever sent from code:
  marketing-lead substack publish --draft-id N --approval-ref REF
- Subscriber lists and exports are not available to you. Never ask for or store emails.
