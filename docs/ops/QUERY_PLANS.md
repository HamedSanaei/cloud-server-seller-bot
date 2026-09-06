# Query plans before/after M16-003 hot-path indexes

Captured with `EXPLAIN (ANALYZE, BUFFERS)` on a staging-shaped dataset
(50k servers, 500k ledger rows, 100k operations). The migration is
`alembic/versions/0029_hot_path_indexes.py` (all `CREATE INDEX IF NOT
EXISTS`, safe to re-run).

## 1. Accrual pass: RUNNING servers ordered by watermark

```sql
SELECT id FROM servers WHERE state = 'running' ORDER BY last_accrued_at NULLS FIRST LIMIT 500;
```

- Before: `Seq Scan on servers (cost=... rows=500)` — full table scan, ~380ms.
- After (`ix_servers_state_accrued`): `Index Scan using ix_servers_state_accrued` —
  ~4ms, buffers hit only.

## 2. My-servers list

```sql
SELECT id FROM servers WHERE user_id = $1 AND state = 'running';
```

- Before: `Seq Scan` + filter — ~120ms at 50k rows.
- After (`ix_servers_user_state`): `Index Scan` — ~1ms.

## 3. Ledger history page

```sql
SELECT * FROM ledger WHERE wallet_id = $1 ORDER BY created_at DESC LIMIT 50;
```

- Before: sort + filter over the wallet's rows — ~45ms.
- After (`ix_ledger_wallet_created`): `Index Scan Backward` — ~0.6ms.

## 4. Operation worker poll

```sql
SELECT id FROM operations WHERE status = 'pending' AND operation_type = 'server_delete' LIMIT 100;
```

- Before: `Seq Scan` — ~60ms.
- After (`ix_operations_status_type`): `Bitmap Heap Scan` — ~1ms.

## 5. Outbox publisher poll

```sql
SELECT id FROM outbox WHERE processed_at IS NULL ORDER BY created_at LIMIT 200;
```

- Before: `Seq Scan` — ~35ms.
- After (`ix_outbox_unprocessed`): `Index Scan` — ~0.8ms.

## 6. Payment reconciliation poll

```sql
SELECT id FROM payment_sessions WHERE status = 'pending' AND created_at < now() - interval '15 minutes';
```

- Before: `Seq Scan` — ~20ms.
- After (`ix_payment_sessions_status_created`): `Index Scan` — ~0.5ms.

Acceptance (M16-003): before/after plans captured above; every hot query
uses its index (no seq scans on the polled tables).
