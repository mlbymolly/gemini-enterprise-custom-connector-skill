-- =====================================================================
-- 01_create_role_and_warehouse.sql
-- Bootstraps a least-privilege role and warehouse for the Gemini
-- Enterprise <-> Snowflake MCP connector.
--
-- Run as ACCOUNTADMIN (or a role with CREATE ROLE / CREATE WAREHOUSE).
-- =====================================================================

USE ROLE ACCOUNTADMIN;

-- ---------- Dedicated role for Gemini Enterprise users ---------------
CREATE ROLE IF NOT EXISTS GE_AGENT_ROLE
  COMMENT = 'Role assumed by Gemini Enterprise end-users via External OAuth';

-- ---------- Warehouse the agent will run queries on ------------------
CREATE WAREHOUSE IF NOT EXISTS GE_AGENT_WH
  WITH WAREHOUSE_SIZE = 'XSMALL'
       AUTO_SUSPEND   = 60
       AUTO_RESUME    = TRUE
       INITIALLY_SUSPENDED = TRUE
       COMMENT = 'Warehouse used by Gemini Enterprise agents';

GRANT USAGE   ON WAREHOUSE GE_AGENT_WH TO ROLE GE_AGENT_ROLE;
GRANT OPERATE ON WAREHOUSE GE_AGENT_WH TO ROLE GE_AGENT_ROLE;

-- ---------- Read access to the sales data ----------------------------
-- Replace SALES_DB / ANALYTICS with your actual database and schema.
GRANT USAGE  ON DATABASE SALES_DB                TO ROLE GE_AGENT_ROLE;
GRANT USAGE  ON SCHEMA   SALES_DB.ANALYTICS      TO ROLE GE_AGENT_ROLE;
GRANT SELECT ON ALL    TABLES IN SCHEMA SALES_DB.ANALYTICS TO ROLE GE_AGENT_ROLE;
GRANT SELECT ON FUTURE TABLES IN SCHEMA SALES_DB.ANALYTICS TO ROLE GE_AGENT_ROLE;
GRANT SELECT ON ALL    VIEWS  IN SCHEMA SALES_DB.ANALYTICS TO ROLE GE_AGENT_ROLE;
GRANT SELECT ON FUTURE VIEWS  IN SCHEMA SALES_DB.ANALYTICS TO ROLE GE_AGENT_ROLE;

-- ---------- Cortex usage so the MCP server can call Cortex tools -----
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE GE_AGENT_ROLE;
