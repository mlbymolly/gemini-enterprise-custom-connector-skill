-- =====================================================================
-- 02_external_oauth_integration.sql
-- Creates the External OAuth integration so JWTs issued by your IdP
-- (shown here for Google Cloud Identity / Workspace -- the Gemini
-- Enterprise tenant IdP) can be exchanged for Snowflake sessions.
--
-- Replace the placeholders:
--   <ISSUER_URL>          e.g. https://accounts.google.com
--   <JWS_KEYS_URL>        e.g. https://www.googleapis.com/oauth2/v3/certs
--   <AUDIENCE>            the OAuth client / audience configured in
--                         Gemini Enterprise's connector registration.
--
-- If you use Azure Entra ID instead, set:
--   ISSUER_URL    = https://sts.windows.net/<tenant-id>/
--   JWS_KEYS_URL  = https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys
--   AUDIENCE      = api://<app-id>
-- =====================================================================

USE ROLE ACCOUNTADMIN;

CREATE OR REPLACE SECURITY INTEGRATION GE_EXTERNAL_OAUTH
  TYPE                              = EXTERNAL_OAUTH
  ENABLED                           = TRUE
  EXTERNAL_OAUTH_TYPE               = CUSTOM
  EXTERNAL_OAUTH_ISSUER             = '<ISSUER_URL>'
  EXTERNAL_OAUTH_JWS_KEYS_URL       = '<JWS_KEYS_URL>'
  EXTERNAL_OAUTH_AUDIENCE_LIST      = ('<AUDIENCE>')
  EXTERNAL_OAUTH_TOKEN_USER_MAPPING_CLAIM = 'upn'   -- or 'email' / 'sub'
  EXTERNAL_OAUTH_SNOWFLAKE_USER_MAPPING_ATTRIBUTE  = 'LOGIN_NAME'
  EXTERNAL_OAUTH_ANY_ROLE_MODE      = 'ENABLE'
  COMMENT = 'External OAuth for Gemini Enterprise user-delegated access';

-- Quick sanity-check the integration
DESC SECURITY INTEGRATION GE_EXTERNAL_OAUTH;
