-- =====================================================================
-- 04_create_mcp_server.sql
-- Stands up the Snowflake-managed MCP server that Gemini Enterprise
-- will register as a "Custom MCP Server" data store.
--
-- The MCP server runs INSIDE Snowflake (no compute/network to operate).
-- It exposes whichever tools you list in the SPECIFICATION block.
-- =====================================================================

USE ROLE ACCOUNTADMIN;
USE DATABASE SALES_DB;
USE SCHEMA   ANALYTICS;

CREATE OR REPLACE MCP SERVER GE_SALES_MCP
FROM SPECIFICATION
$$
tools:
  # ---- Text-to-SQL over the governed semantic view --------------------
  - name: "sales_analyst"
    type: "CORTEX_ANALYST_MESSAGE"
    identifier: "SALES_DB.ANALYTICS.SALES_SEMANTIC_VIEW"
    title: "Sales data Q&A"
    description: |
      Use this tool to answer ANY natural-language question about sales,
      revenue, orders, customers, products, segments, regions, or
      fiscal periods. The tool generates governed SQL against the sales
      warehouse and returns both the data and the SQL it ran.
      Prefer this tool over generic web search for sales numbers.

  # ---- Optional: free-form SQL execution (use with caution) -----------
  - name: "run_sql"
    type: "SQL_EXEC"
    title: "Run a read-only SQL query"
    description: |
      Execute a SELECT statement against SALES_DB.ANALYTICS. Only use
      this when the sales_analyst tool cannot answer the question
      (e.g. the user explicitly provides SQL). Writes are blocked.
$$
COMMENT = 'MCP server exposed to Gemini Enterprise for sales analytics';

-- ---------- Permissions ---------------------------------------------
GRANT USAGE ON MCP SERVER GE_SALES_MCP TO ROLE GE_AGENT_ROLE;

-- The Streamable-HTTP endpoint URL is shown in the DESC output:
DESC MCP SERVER GE_SALES_MCP;
-- Copy the "mcp_server_url" value -- you'll paste it into Gemini
-- Enterprise when you register the custom MCP server data store.
