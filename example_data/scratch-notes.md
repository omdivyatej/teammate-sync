# Scratch notes — Account migration

## Trigger disable script (run before migration)

```sql
UPDATE TriggerHandler__c
SET Active__c = false
WHERE Object__c = 'Account_History__c';
```

## Re-enable after migration

```sql
UPDATE TriggerHandler__c
SET Active__c = true
WHERE Object__c = 'Account_History__c';
```

## Validation rules disabled during migration

- `Account_Required_Industry` — 30% null industry in legacy data.
  Backfill needed before re-enable.

## TODOs

- [ ] Backfill `Industry` on migrated Accounts
- [ ] Re-enable `Account_History__c` trigger after full migration
- [ ] Decide rate-limit scoping (own bucket vs global) — sync with Om
- [ ] AccountTeamMember soft-delete pattern (DO NOT mass-delete on rollback)

## Files modified during this work

- `api/accounts/routes.py`
- `api/accounts/serializers.py`
- `migrations/0042_account_migration.sql`
- `api/middleware/auth.py` (added Source_Id__c lookup helper)

## Migration stats

- Total Account rows: ~2.4M
- First batch migrated: 500k (clean)
- Estimated total time: 4-5 hours of migration runtime
