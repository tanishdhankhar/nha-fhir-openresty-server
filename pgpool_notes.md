# pgpool connection exhaustion (2026-09-25)

## Symptom

`GET /packages` on the catalogue API returned 500. asyncpg failed while opening a connection:

```
asyncpg.exceptions.InternalServerError: unable to read message kind
DETAIL:  kind does not match between main(53) slot[1] (45)
```

## What the error means

This is a pgpool-II error, not an app or asyncpg error. The API connects to
`psql-postgresql-ha-pgpool.postgres.svc.cluster.local` (Bitnami postgresql-ha chart).

- `main(53)`: the primary answered with `'S'` (ParameterStatus), so it accepted the connection.
- `slot[1] (45)`: backend node 1 (`psql-postgresql-ha-postgresql-1`) answered with `'E'` (ErrorResponse).

The replies differ, so pgpool drops the connection.

## Root cause

Node 1 was out of connection slots. Its log showed:

```
FATAL: remaining connection slots are reserved for roles with the SUPERUSER attribute
FATAL: sorry, too many clients already
```

`max_connections` is 300. `pg_stat_activity` on node 1 showed about 300 connections, nearly all `idle`
and coming from the two pgpool pod IPs. They were pgpool's cached backend connections:

| user                      | approx. connections              |
| ------------------------- | -------------------------------- |
| sandbox_user              | ~203 (one `idle in transaction`) |
| hapi_user                 | ~63                              |
| nha_keycloak_user         | ~11                              |
| postgres / repmgr / other | ~20                              |

The pgpool settings do not fit inside the Postgres limit:

```
PGPOOL_NUM_INIT_CHILDREN=200
PGPOOL_MAX_POOL=2
2 replicas × 200 children × 2 max_pool = up to 800 backend connections per node
Postgres max_connections = 300
```

`connection_life_time`, `child_max_connections` and `client_idle_limit` are unset, so pgpool keeps a
cached backend connection until its child process exits. Idle connections only accumulate after
traffic bursts (mostly `sandbox_user`) until node 1 is full. A redeploy clears them, which is why it
comes back after a while.

## Short-term fix

Rolling restart of pgpool. This is temporary.

```bash
kubectl -n postgres rollout restart deploy/psql-postgresql-ha-pgpool
```

## Long-term fix (in helm values, so a redeploy does not undo it)

1. Size pgpool to fit Postgres. Keep `replicas × numInitChildren × maxPool` at about 260 or less
   (leave ~40 of the 300 slots for repmgr, superuser and direct connections).
   Example: `numInitChildren: 60`, `maxPool: 2` on 2 replicas = 240 worst case.
   Check peak concurrent clients first (`show pool_processes`, count rows with a non-empty database)
   so real traffic is not starved.
2. Make pgpool recycle idle connections: set `connection_life_time` (~300s) via `pgpool.configuration`
   and set `childLifeTime`. Check `helm show values` for what the chart version exposes.
   Avoid `clientIdleLimit`; it disconnects idle clients and can break apps with client-side pools (HAPI/Hikari).
3. Fix the heavy client. `sandbox_user` holds ~3x HAPI's connections. Cap its client-side pool and find
   the session stuck `idle in transaction`. Consider `idle_in_transaction_session_timeout` for that role.
4. Add monitoring. Alert when connections on any node pass ~80% of `max_connections`.
5. Raise `postgresql.maxConnections` only as a last step. It costs memory on every Postgres pod, needs a
   rolling restart, and only moves the ceiling.

Also check `postgresql-0` and `postgresql-2` with the same query. The primary is likely close to its
limit too; node 1 just tipped over first.

## Useful commands

Run the query inside the pod so the password never appears on the command line:

```bash
kubectl -n postgres exec -it psql-postgresql-ha-postgresql-1 -c postgresql -- \
  sh -c 'PGPASSWORD="$POSTGRESQL_POSTGRES_PASSWORD" psql -U postgres -c "
SELECT usename, application_name, client_addr, state, count(*)
FROM pg_stat_activity GROUP BY 1,2,3,4 ORDER BY 5 DESC;" -c "SHOW max_connections;"'
```

Variable names may differ by chart version. List names only (no values):

```bash
kubectl -n postgres exec psql-postgresql-ha-postgresql-1 -c postgresql -- \
  sh -c 'env | cut -d= -f1 | grep -i pass'
```

Pgpool pool settings currently in effect:

```bash
kubectl -n postgres exec deploy/psql-postgresql-ha-pgpool -- \
  env | grep -iE "PGPOOL_(NUM_INIT_CHILDREN|MAX_POOL|CHILD_LIFE|CHILD_MAX|CONNECTION_LIFE|CLIENT_IDLE)"
```

## Other observations

- Pgpool image is `bitnamilegacy/pgpool:4.6.0-debian-12-r2`, which no longer gets updates.
- The `regcred` image pull secret is missing in the `postgres` namespace. Harmless while the image is
  cached on the node, but a pod scheduled to a node without it could fail to pull.

---

## Sept. 28, 2026 (Monday)

```bash
kubectl -n postgres exec -it psql-postgresql-ha-postgresql-1 -c postgresql -- \
  env PGPASSWORD='$PGPASSWORD' psql -U postgres -c "
SELECT usename, application_name, client_addr, state, count(*)
FROM pg_stat_activity
GROUP BY 1, 2, 3, 4
ORDER BY 5 DESC;" -c "SHOW max_connections;"
```

| usename              | application_name                | client_addr    | state               | count |
| -------------------- | ------------------------------- | -------------- | ------------------- | ----: |
| sandbox_user         |                                 | 10.119.111.244 | idle                |   148 |
| sandbox_user         |                                 | 10.119.161.190 | idle                |    48 |
| hapi_user            |                                 | 10.119.111.244 | idle                |    43 |
| hapi_user            |                                 | 10.119.161.190 | idle                |    17 |
| postgres             |                                 | 10.119.111.244 | idle                |    10 |
| hapi_user            | PostgreSQL JDBC Driver          | 10.119.111.244 | idle                |     8 |
|                      |                                 |                |                     |     5 |
| postgres             | psql                            | 10.119.161.190 | idle                |     3 |
| new_code_editor_user |                                 | 10.119.161.190 | idle                |     2 |
| hapi_user            | PostgreSQL JDBC Driver          | 10.119.161.190 | idle                |     2 |
| repmgr               | repmgr                          | 10.119.160.129 | idle                |     2 |
| nha_keycloak_user    | PostgreSQL JDBC Driver          | 10.119.161.190 | idle                |     2 |
| postgres             |                                 | 10.119.161.190 | idle                |     2 |
| repmgr               | psql-postgresql-ha-postgresql-0 | 10.119.113.223 | active              |     1 |
| postgres             | psql                            |                | active              |     1 |
| sandbox_user         |                                 | 10.119.161.190 | idle in transaction |     1 |
| nha_superset_user    |                                 | 10.119.111.244 | idle                |     1 |
| postgres             |                                 | 127.0.0.1      | idle                |     1 |
| repmgr               | repmgr                          | 10.119.103.214 | idle                |     1 |
| nha_keycloak_user    |                                 | 10.119.111.244 | idle                |     1 |
| repmgr               | repmgr                          | 10.119.113.223 | idle                |     1 |
| postgres             |                                 |                |                     |     1 |
| repmgr               | psql-postgresql-ha-postgresql-2 | 10.119.160.129 | active              |     1 |

(23 rows)

### `max_connections`

| setting           | value |
| ----------------- | ----: |
| `max_connections` |   300 |

(1 row)
