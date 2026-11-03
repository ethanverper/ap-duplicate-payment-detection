-- =============================================================================
-- 02_duplicate_review_list.sql — the AP analyst's work list: every suspect payment, biggest first.
--
-- Business question: which payments do I stop or recover today, and which do I check first?
-- Owner: AP analyst.
-- =============================================================================

SELECT
    confidence,
    rule,
    dt AS paid_on,
    entity,
    supplier_id,

    -- The two payments side by side: the second one is the suspect
    original_payment_id,
    original_invoice_number,
    suspect_payment_id,
    suspect_invoice_number,

    -- The money, in the invoice currency and in USD
    invoice_amount,
    currency,
    amount_at_risk,
    amount_at_risk_usd
FROM gold_duplicate_suspects
-- HIGH first (proven overpayments), then the biggest amounts
ORDER BY CASE WHEN confidence = 'HIGH' THEN 0 ELSE 1 END, amount_at_risk_usd DESC
