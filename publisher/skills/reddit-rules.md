---
name: reddit-rules
description: Reddit posting limits and self-promotion etiquette
---
# Reddit rules

- Post only to subreddits in config reddit.allowedSubreddits, at most reddit.dailyPostCap a day.
  Both are enforced in code; an empty allowlist means nothing can be posted.
- Read the subreddit rules first. If self-promotion is restricted or needs a flair or
  mod approval, do not post: draft and tell Hermes.
- Lead with something useful for the community, not a link drop. Disclose that you work on
  buildmy.house when relevant. One post per topic; never repost after a removal.
- No vote or comment manipulation, no multiple accounts, no DMs to users.
- Reddit API access and the self-promotion policy are the account owner's responsibility
  (approved app registration). If the API returns an error, report it; do not retry in a loop.
- Command: publisher reddit post --subreddit S --title T --text B --approval-ref REF
