// Named read queries over the marketing schema. The agent chooses a query name and bounded
// parameters; it never writes SQL. Runs psql with PUBLISHER_DATABASE_URL (publisher_reader: SELECT only).
export const QUERIES = {
  "ingest-status": { sql: "SELECT source, status, rows_upserted, window_start, window_end, left(coalesce(error,''),120) AS note, started_at FROM marketing.ingest_runs ORDER BY started_at DESC LIMIT :limit", defaults: { limit: 20 } },
  "top-pages": { sql: "SELECT page_path, sum(sessions) AS sessions, sum(total_users) AS users, sum(screen_page_views) AS views FROM marketing.ga4_daily WHERE date >= current_date - :days GROUP BY 1 ORDER BY 2 DESC LIMIT :limit", defaults: { days: 28, limit: 20 } },
  "top-queries": { sql: "SELECT query, sum(clicks) AS clicks, sum(impressions) AS impressions, round(avg(position)::numeric,1) AS avg_position FROM marketing.gsc_daily WHERE date >= current_date - :days GROUP BY 1 ORDER BY 3 DESC LIMIT :limit", defaults: { days: 28, limit: 20 } },
  "reddit-posts": { sql: "SELECT DISTINCT ON (post_id) subreddit, title, score, num_comments, snapshot_date FROM marketing.reddit_posts_daily ORDER BY post_id, snapshot_date DESC LIMIT :limit", defaults: { limit: 20 } },
  "substack-posts": { sql: "SELECT DISTINCT ON (post_id) title, post_date, opens, open_rate, views, subscribes, unsubscribes, snapshot_date FROM marketing.substack_post_stats ORDER BY post_id, snapshot_date DESC LIMIT :limit", defaults: { limit: 20 } },
  "substack-publication": { sql: "SELECT snapshot_date, subscribers, subscribers_last_30_days, views_30d, open_rate_30d FROM marketing.substack_publication_daily ORDER BY snapshot_date DESC LIMIT :limit", defaults: { limit: 14 } },
};

export function buildSql(name, params = {}) {
  const q = QUERIES[name];
  if (!q) throw new Error(`unknown query "${name}"; known: ${Object.keys(QUERIES).join(", ")}`);
  const merged = { ...q.defaults, ...params };
  for (const [k, v] of Object.entries(merged)) {
    if (!(k in q.defaults)) throw new Error(`unknown parameter "${k}" for ${name}`);
    if (!Number.isInteger(v) || v < 1 || v > 365) throw new Error(`parameter ${k} must be an integer 1..365`);
  }
  return q.sql.replace(/:(\w+)/g, (_, k) => String(merged[k]));
}
