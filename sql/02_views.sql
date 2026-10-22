-- Reporting views on top of the loaded data.
SET search_path TO procurement;

-- Supplier on-time delivery (OTD), only for lines already received.
CREATE OR REPLACE VIEW vw_supplier_otd AS
SELECT
    s.supplier_code,
    s.supplier_name,
    s.country,
    COUNT(*)                                             AS received_lines,
    COUNT(*) FILTER (WHERE f.on_time)                    AS on_time_lines,
    ROUND(100.0 * COUNT(*) FILTER (WHERE f.on_time) / COUNT(*), 1) AS otd_pct,
    ROUND(AVG(f.days_late) FILTER (WHERE f.days_late > 0), 1)      AS avg_days_late_when_late,
    ROUND(SUM(f.amount_usd), 2)                          AS spend_usd
FROM fact_po_line f
JOIN dim_supplier s USING (supplier_code)
WHERE f.received_date IS NOT NULL
GROUP BY s.supplier_code, s.supplier_name, s.country;

-- Monthly spend by category, with share of the month's total.
CREATE OR REPLACE VIEW vw_spend_by_category_month AS
WITH monthly AS (
    SELECT
        date_trunc('month', order_date)::date AS order_month,
        category,
        SUM(amount_usd)                       AS spend_usd,
        COUNT(*)                              AS po_lines
    FROM fact_po_line
    GROUP BY 1, 2
)
SELECT
    order_month,
    category,
    ROUND(spend_usd, 2) AS spend_usd,
    po_lines,
    ROUND(100.0 * spend_usd / SUM(spend_usd) OVER (PARTITION BY order_month), 1) AS pct_of_month
FROM monthly;

-- Open lines: ordered, not yet received, and already past the promised date.
CREATE OR REPLACE VIEW vw_open_past_due AS
SELECT
    f.po_number,
    f.line_number,
    f.supplier_code,
    s.supplier_name,
    f.item_code,
    f.promised_date,
    (CURRENT_DATE - f.promised_date) AS days_past_due,
    f.amount_usd
FROM fact_po_line f
JOIN dim_supplier s USING (supplier_code)
WHERE f.received_date IS NULL
  AND f.promised_date < CURRENT_DATE;
