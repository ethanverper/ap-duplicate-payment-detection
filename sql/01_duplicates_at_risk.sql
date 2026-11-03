-- =============================================================================
-- 01_duplicates_at_risk.sql — KPI 1: $ at risk from duplicate payments, Entity → Category → Supplier.
--
-- Business question: how much money may have been paid twice, where, and how sure are we?
-- Owner: AP manager / controller.
--
-- GROUP BY ROLLUP adds a subtotal for every level of the hierarchy in one query. Portable SQL:
-- runs on Athena (Trino) and on Spark SQL (tests/glue_jobs/test_sql.py runs it locally).
-- =============================================================================

SELECT
    -- Which level of the hierarchy this row is (a rolled-up column comes back as NULL)
    CASE
        WHEN entity IS NULL THEN '0 All entities'
        WHEN category IS NULL THEN '1 Entity'
        WHEN supplier_id IS NULL THEN '2 Category'
        ELSE '3 Supplier'
    END AS level,
    entity,
    category,
    supplier_id,

    -- How many suspects, split by how sure we are
    COUNT(*) AS suspects,
    SUM(CASE WHEN confidence = 'HIGH' THEN 1 ELSE 0 END) AS high_confidence,

    -- The money, in USD: proven (HIGH) and to review (MEDIUM)
    ROUND(SUM(amount_at_risk_usd), 2) AS at_risk_usd,
    ROUND(SUM(CASE WHEN confidence = 'HIGH' THEN amount_at_risk_usd ELSE 0 END), 2) AS proven_overpaid_usd,
    ROUND(SUM(CASE WHEN confidence = 'MEDIUM' THEN amount_at_risk_usd ELSE 0 END), 2) AS to_review_usd
FROM gold_duplicate_suspects
GROUP BY ROLLUP (entity, category, supplier_id)
ORDER BY entity NULLS FIRST, category NULLS FIRST, at_risk_usd DESC
