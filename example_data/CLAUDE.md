# Account Object Migration — Penguin → Stratus

## Project context

Migrating the Account object from the legacy Salesforce org (Penguin) to the
new org (Stratus). Includes dependent fields, ownership, history tracking,
and the AccountTeamMember relationship.

Volume: ~2.4M Account rows total. First batch (500k) migrated cleanly.

## Decisions made

### Pagination: cursor-based, NOT offset-based
Reason: Account table has 2.4M rows; offset pagination produced inconsistent
results during concurrent writes from the integration user. Cursor pagination
uses `Id` as the cursor and processes in deterministic order. Apply the same
pattern to dependent objects (Contact, Opportunity).

### External ID: keeping `Source_Id__c` on Account
Reason: downstream objects (Contact, Opportunity, AccountTeamMember) use it
for lookup resolution. **Do NOT remove this field** even if it looks unused.

### Auth pattern: existing JWT middleware
Don't roll a new auth path for the migration endpoints. The existing
`api/middleware/auth.py` already handles the integration user correctly —
just added a Source_Id__c lookup helper to it.

## Gotchas (read before you migrate dependent objects)

### Recursive trigger on Account_History__c
When migrating, the History trigger fires on insert and tries to write to a
table that's mid-migration → infinite loop. **Disable the trigger manually
before the migration run, re-enable after.** Exact SOQL is in
`scratch-notes.md`.

Expect the same pattern on `Contact_History__c`, `Opportunity_History__c`.

### AccountTeamMember cascading delete
Do NOT mass-delete AccountTeamMember rows on rollback. The cascade hits user
permissions and is painful to recover. Use soft-delete: set `Active__c = false`.

### Validation rules during migration
Disabled VR `Account_Required_Industry` because ~30% of legacy data has null
industry. **TODO: backfill industry before re-enabling.**

## Open questions

- **Rate limits on `/api/accounts`**: should they inherit the global bucket
  or have their own? Not yet decided — affects Contact, Opportunity endpoints
  too. Worth syncing with Om before committing.

## Files touched

- `api/accounts/routes.py` (new)
- `api/accounts/serializers.py` (new)
- `migrations/0042_account_migration.sql` (new)
- `api/middleware/auth.py` (modified — added Source_Id__c lookup helper)

## Bulk API decision
Decided to use Salesforce Bulk API for any object with more than 100k rows. Tested on Account.

## Phase 3a live test
Confirmed S3 round-trip works at 02:50

## Live proof at 03:01:24
If this shows up in the MCP, the full daemon → S3 → MCP loop works.
