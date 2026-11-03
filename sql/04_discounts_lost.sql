-- =============================================================================
-- 04_discounts_lost.sql — KPI 4: early-payment discounts lost, Entity → Category → Supplier.
--
-- Business question: how much did we throw away by paying after the discount window, and where?
-- Owner: Treasury.
-- =============================================================================

SELECT
    CASE
        WHEN entity IS NULL THEN '0 All entities'
        WHEN category IS NULL THEN '1 Entity'
        WHEN supplier_id IS NULL THEN '2 Category'
        ELSE '3 Supplier'
    END AS level,
    entity,
    category,
    supplier_id,

    -- How many invoices lost their discount, on how much spend, and what it cost
    COUNT(*) AS invoices,
    ROUND(SUM(invoice_amount_usd), 2) AS invoice_amount_usd,
    ROUND(SUM(discount_lost_usd), 2) AS discount_lost_usd
FROM gold_discounts_lost
GROUP BY ROLLUP (entity, category, supplier_id)
ORDER BY entity NULLS FIRST, category NULLS FIRST, discount_lost_usd DESC
