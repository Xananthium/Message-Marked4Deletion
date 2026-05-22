#!/bin/bash
# Canary step 7: verify multi-turn re-attach after follow-up email
# Usage: ./canary_step7.sh <issue_id> <thread_id>

set -euo pipefail

ISSUE_ID="${1:?issue_id required}"
THREAD_ID="${2:?thread_id required}"
DB="postgres://paperclip:3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730@127.0.0.1:5432/paperclip"
COMPANY_ID="3f6ac8c4-e9ec-4fd3-b644-b7cb5d15bfa6"
MERCER_ID="cfaac33f-c89a-43d6-95dd-2a9587d1d69d"
PG="psql $DB -t -A"

echo "=== STEP 7: Waiting for poller to re-attach follow-up email ==="
echo "Watching for new comment on issue $ISSUE_ID with thread $THREAD_ID"

REATTACHED=false
DUPLICATE=false
for i in $(seq 1 12); do
    echo "Poll attempt $i/12 ($(date))..."
    
    # Check comment count on original issue
    COMMENT_COUNT=$($PG -c "
        SELECT COUNT(*) FROM issue_comments
        WHERE issue_id = '$ISSUE_ID'::uuid
          AND author_user_id = 'customer'
    " 2>/dev/null || echo "0")
    echo "  Customer comments on original issue: $COMMENT_COUNT"
    
    # Check for duplicate issue with same thread
    DUP_COUNT=$($PG -c "
        SELECT COUNT(DISTINCT i.id) FROM issues i
        JOIN issue_comments ic ON ic.issue_id = i.id
        WHERE i.origin_kind='customer_email'
          AND ic.metadata->>'gmail_thread_id' = '$THREAD_ID'
          AND i.created_at > now() - interval '30 min'
    " 2>/dev/null || echo "0")
    echo "  Issues with thread_id in last 30 min: $DUP_COUNT"
    
    if [ "$DUP_COUNT" -gt "1" ]; then
        DUPLICATE=true
        echo "FAIL: DUPLICATE ISSUE CREATED! Regression of dedup fix 1adf142"
        break
    fi
    
    if [ "$COMMENT_COUNT" -gt "1" ]; then
        REATTACHED=true
        echo "PASS: Follow-up re-attached to original issue!"
        echo "  Customer comments: $COMMENT_COUNT"
        
        # Show all comments
        echo "--- Comment chain ---"
        $PG -c "
            SELECT ic.author_type, ic.author_user_id,
                   left(ic.body, 150) as body_preview,
                   ic.metadata->>'outbound' as outbound,
                   ic.metadata->>'awaiting_customer_reply' as awaiting,
                   ic.created_at
            FROM issue_comments ic
            WHERE ic.issue_id = '$ISSUE_ID'::uuid
            ORDER BY ic.created_at ASC
        " 2>/dev/null
        echo "--- End comment chain ---"
        break
    fi
    
    echo "  No re-attach yet. Waiting 30s..."
    sleep 30
done

if [ "$REATTACHED" = false ] && [ "$DUPLICATE" = false ]; then
    echo "FAIL: Follow-up not re-attached after 6 minutes."
fi

echo ""
echo "=== STEP 8 (optional): Wake Mercer for second turn ==="
echo "To run step 8:"
echo "  psql $DB -c \"UPDATE agents SET status='running' WHERE id='$MERCER_ID';\""
echo "  psql $DB -c \"INSERT INTO agent_wakeup_requests (company_id, agent_id, source, reason) VALUES ('$COMPANY_ID'::uuid, '$MERCER_ID'::uuid, 'operator', 'canary step 8 second turn');\""
echo "Then watch issue_comments for Mercer's second reply."

echo ""
echo "=== STEP 9: Cleanup ==="
$PG -c "
    INSERT INTO issue_comments (company_id, issue_id, author_user_id, author_type, body, metadata)
    VALUES ('$COMPANY_ID'::uuid, '$ISSUE_ID'::uuid, 'operator', 'user',
            '[canary 2026-05-19: PASS -- multi-turn verification complete]',
            '{\"canary\": true, \"date\": \"2026-05-19\"}');
" 2>/dev/null
echo "Canary comment added."

$PG -c "
    UPDATE issues SET status='done', completed_at=now()
    WHERE id='$ISSUE_ID'::uuid;
" 2>/dev/null
echo "Issue marked done."

$PG -c "
    UPDATE agents SET status='paused', pause_reason='canary 2026-05-19 complete'
    WHERE id='$MERCER_ID';
" 2>/dev/null
echo "Mercer paused."
echo ""
echo "Canary cleanup complete."
