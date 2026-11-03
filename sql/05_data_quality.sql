-- =============================================================================
-- 05_data_quality.sql — did every file row end up somewhere? One row per payment date.
--
-- Business question: can we trust today's numbers?
-- Owner: Data engineering.
--
-- file rows = curated + duplicate rows + quarantined, or the Glue job would have failed before writing.
-- =============================================================================

SELECT
    dt,
    file_rows,
    curated_rows,
    duplicate_rows,
    quarantined_rows,
    is_balanced
FROM gold_data_quality
ORDER BY dt
