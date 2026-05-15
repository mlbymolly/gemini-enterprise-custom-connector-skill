-- =====================================================================
-- 03_semantic_view.sql
-- Defines a Cortex Analyst SEMANTIC VIEW over the sales tables.
-- Cortex Analyst is the text-to-SQL engine that the MCP server will
-- expose to Gemini Enterprise as a tool. Semantic views are now the
-- recommended way to describe business concepts (the older YAML-on-
-- stage semantic models still work but are legacy).
--
-- Adjust the table/column names to your actual schema.
-- =====================================================================

USE ROLE GE_AGENT_ROLE;             -- or a role that can CREATE in SALES_DB
USE WAREHOUSE GE_AGENT_WH;
USE DATABASE SALES_DB;
USE SCHEMA   ANALYTICS;

CREATE OR REPLACE SEMANTIC VIEW SALES_SEMANTIC_VIEW
  TABLES (
    orders      AS SALES_DB.ANALYTICS.FACT_ORDERS
                  PRIMARY KEY (order_id),
    customers   AS SALES_DB.ANALYTICS.DIM_CUSTOMERS
                  PRIMARY KEY (customer_id),
    products    AS SALES_DB.ANALYTICS.DIM_PRODUCTS
                  PRIMARY KEY (product_id),
    dates       AS SALES_DB.ANALYTICS.DIM_DATE
                  PRIMARY KEY (date_key)
  )
  RELATIONSHIPS (
    orders (customer_id) REFERENCES customers (customer_id),
    orders (product_id)  REFERENCES products  (product_id),
    orders (order_date)  REFERENCES dates     (date_key)
  )
  FACTS (
    orders.gross_revenue  AS gross_amount,
    orders.discount       AS discount_amount,
    orders.units          AS quantity
  )
  DIMENSIONS (
    customers.segment        AS segment        WITH SYNONYMS ('customer segment','tier'),
    customers.region         AS region         WITH SYNONYMS ('geo','territory'),
    products.category        AS category       WITH SYNONYMS ('product category'),
    products.sku             AS sku,
    dates.fiscal_quarter     AS fiscal_quarter,
    dates.fiscal_year        AS fiscal_year,
    dates.calendar_month     AS calendar_month
  )
  METRICS (
    orders.net_revenue       AS SUM(gross_amount - discount_amount)
                                WITH SYNONYMS ('revenue','net sales','sales'),
    orders.total_orders      AS COUNT(DISTINCT order_id)
                                WITH SYNONYMS ('order count'),
    orders.units_sold        AS SUM(quantity)
                                WITH SYNONYMS ('units','volume'),
    orders.avg_order_value   AS SUM(gross_amount) / COUNT(DISTINCT order_id)
                                WITH SYNONYMS ('AOV')
  )
  COMMENT = 'Business-friendly semantic layer over the sales star-schema.
             Use this to answer revenue, order-count and segmentation
             questions in natural language.';
