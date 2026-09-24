# Power Automate flows (standard connectors only)

Uses **SharePoint**, **Microsoft Teams** and built-in actions (Parse JSON, Compose, Condition).
Do **not** use "When an HTTP request is received" or the HTTP action: both are premium.
Build the flows under a team member's account (or a shared service account if IT provides one),
and point them at the team SharePoint library that holds `AI-Forge-Shared`.

## Flow 1 – "AI Forge: outbox → Teams"

1. **Trigger:** SharePoint · *When a file is created in a folder*
   - Site: your team site · Folder: `/AI-Forge-Shared/outbox`
2. **Get file content** (SharePoint) → **Parse JSON** with the schema from
   `forge/assets/schemas/outbox.schema.json` (generate from a sample file).
3. **Condition:** `wait_for_response` is true
   - **Yes:** Teams · *Post adaptive card and wait for a response*
     - Post as: Flow bot · Post in: Channel · Team/Channel: your support channel
     - Message: `@{body('Parse_JSON')?['card']}`
     - Then **Compose** the decision:
       ```json
       {
         "schema": 1,
         "ticket_key": "@{body('Parse_JSON')?['ticket_key']}",
         "request_id": "@{body('Post_adaptive_card_and_wait_for_a_response')?['data']?['request_id']}",
         "action": "@{body('Post_adaptive_card_and_wait_for_a_response')?['data']?['action']}",
         "comment": "@{body('Post_adaptive_card_and_wait_for_a_response')?['data']?['comment']}",
         "responder": "@{body('Post_adaptive_card_and_wait_for_a_response')?['responder']?['userPrincipalName']}",
         "responded_at": "@{utcNow()}"
       }
       ```
       Check the exact output property names in your tenant's run history once, then adjust.
     - SharePoint · **Create file** in `/AI-Forge-Shared/decisions`,
       name `@{body('Parse_JSON')?['ticket_key']}__@{body('Parse_JSON')?['request_id']}.json`,
       content = Compose output.
   - **No:** Teams · *Post card in a chat or channel* (or *Post message* when `kind` = notify, using `text`).
4. **Delete file** (SharePoint): the outbox item.

Notes:
- The responder comes from Teams identity, not from card input. Runners also check it against `approvers`.
- A waiting run can stay open for a long time, but not forever (flow run duration limits apply).
  Runners re-post stale approvals, and a late click on an old card is ignored because its `request_id` is no longer pending.
- Set the trigger's concurrency to allow parallel runs, so one waiting card doesn't block others.

## Flow 2 – "AI Forge: weekly digest" (optional)
Trigger: *Recurrence* (Friday 16:00) → SharePoint *List folder* `runners/` → loop: *Get file content*
→ *Parse JSON* → append to an array → *Create HTML table* → Teams *Post message* with totals:
tickets analyzed, grouped, info-only, duplicates, PRs, rejected, and tokens per person.

## Flow 3 – "AI Forge: stale runner alert" (optional)
Same as flow 2, daily at 10:00. Post an alert when `last_run` is older than 1 working day.
