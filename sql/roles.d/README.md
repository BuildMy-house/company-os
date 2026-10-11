# Database role manifests

One YAML file per Postgres role, reconciled by `scripts/provision-db-roles.sh`.
Adding a role = add one file here + one Infisical key (`password_key`, path
`/hermes`). Format (a deliberately small YAML subset, parsed by awk so it runs
in a stock postgres image):

```yaml
name: my_agent_writer            # role name, [a-z_][a-z0-9_]*, must match filename
login: true                      # true = LOGIN, false = NOLOGIN
password_key: MY_AGENT_WRITER_PASSWORD   # env var / Infisical key holding the password
dsn_key: MY_AGENT_DATABASE_URL           # Infisical/Secret key holding the DSN
grants:
  - schema: company
    tables: [t1, t2]             # explicit tables, or `all` (+ default privileges)
    privileges: [SELECT, INSERT] # SELECT INSERT UPDATE DELETE TRUNCATE
```

Write privileges (INSERT/UPDATE/DELETE/TRUNCATE) not listed for a grant target
are explicitly revoked, so the append-only intent is enforced, not implied.
Passwords must match `[A-Za-z0-9._~-]+` (URL-safe, e.g. `openssl rand -hex 24`).
