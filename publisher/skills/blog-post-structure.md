---
name: blog-post-structure
description: Front matter and section layout for posts in the blog repo
---
# Blog post structure

Posts live in the blog repo (config: publisher/config/publisher.json, today hermees-diary
src/content/blog/YYYY-MM-DD-slug.md). The CLI renders the front matter; you write the body.

Front matter keys: title (max 70 chars), date, category (decisions, research, build-log,
product), tags (3 to 5 lowercase), excerpt (70-170 chars, doubles as meta description).

Body: no H1 (the layout renders the title). Open with the point in two sentences. Use H2
sections: what happened, what we decided, what is next. Link to sources and to our own
earlier posts. 600 to 1200 words. End with one concrete next step, not a slogan.

Submit with: publisher blog pr --file draft.json (title, category, tags, excerpt, body).
It clones the repo, runs the analysis, pushes blog/<slug> and opens a PR. Never push main.
