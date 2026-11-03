-- =============================================================================
-- 03_on_time_and_dpo.sql — KPIs 2 and 3: on-time payment % and DPO, Entity → Category → Supplier.
--
-- Business question: do we pay on time, and how many days do we keep the cash?
-- Owners: AP manager (on time), Treasury (DPO).
--
-- Both are computed AFTER summing: a supplier's on-time % is not the average of its days' %.
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

    -- Counts and money behind the rates
    SUM(settling_payments) AS settling_payments,
    SUM(on_time_payments) AS on_time_payments,
    ROUND(SUM(paid_usd), 2) AS paid_usd,

    -- On-time %, by count and by money (a late $50,000 payment matters more than a late $100 one)
    ROUND(100.0 * SUM(on_time_payments) / NULLIF(SUM(settling_payments), 0), 1) AS on_time_pct,
    ROUND(100.0 * SUM(on_time_paid_usd) / NULLIF(SUM(paid_usd), 0), 1) AS on_time_paid_pct,

    -- DPO: amount-weighted average days from invoice to payment
    ROUND(SUM(days_x_usd) / NULLIF(SUM(paid_usd), 0), 1) AS dpo_days
FROM gold_payment_performance
GROUP BY ROLLUP (entity, category, supplier_id)
ORDER BY entity NULLS FIRST, category NULLS FIRST, on_time_pct
