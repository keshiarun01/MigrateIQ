/* =====================================================================
   MigrateIQ sample domain: P&C insurance carrier
   Source : SQL Server OLTP "PolicyAdmin"   (schema dbo)
   Target : SQL Server EDW "InsuranceDW"    (schemas edw, etl, audit)
   Migration target: AWS (S3 + Glue/PySpark + Iceberg/Delta)
   ===================================================================== */

/* ----------------------------- SOURCE -------------------------------- */
CREATE TABLE dbo.policyholder (
    policyholder_id   INT            NOT NULL PRIMARY KEY,
    first_name        NVARCHAR(50)   NOT NULL,
    last_name         NVARCHAR(50)   NOT NULL,
    date_of_birth     DATE           NULL,
    email             NVARCHAR(100)  NULL,
    state_code        CHAR(2)        NOT NULL,
    created_at        DATETIME2      NOT NULL,
    updated_at        DATETIME2      NOT NULL
);

CREATE TABLE dbo.agent (
    agent_id          INT            NOT NULL PRIMARY KEY,
    agent_code        VARCHAR(10)    NOT NULL,
    agent_name        NVARCHAR(100)  NOT NULL,
    region            VARCHAR(20)    NOT NULL,      -- NORTHEAST, SOUTH, MIDWEST, WEST
    is_active         BIT            NOT NULL
);

CREATE TABLE dbo.product (
    product_id        INT            NOT NULL PRIMARY KEY,
    product_code      VARCHAR(10)    NOT NULL,
    product_name      NVARCHAR(100)  NOT NULL,
    line_of_business  VARCHAR(20)    NOT NULL       -- AUTO, HOME, UMBRELLA
);

CREATE TABLE dbo.policy (
    policy_id         INT            NOT NULL PRIMARY KEY,
    policy_number     VARCHAR(20)    NOT NULL,
    policyholder_id   INT            NOT NULL REFERENCES dbo.policyholder(policyholder_id),
    agent_id          INT            NOT NULL REFERENCES dbo.agent(agent_id),
    product_id        INT            NOT NULL REFERENCES dbo.product(product_id),
    status_code       CHAR(1)        NOT NULL,      -- A=Active C=Cancelled L=Lapsed P=Pending
    effective_date    DATE           NOT NULL,
    expiration_date   DATE           NOT NULL,
    annual_premium    DECIMAL(12,2)  NOT NULL,
    deductible        DECIMAL(10,2)  NOT NULL,
    updated_at        DATETIME2      NOT NULL
);

CREATE TABLE dbo.claim (
    claim_id            INT            NOT NULL PRIMARY KEY,
    claim_number        VARCHAR(20)    NOT NULL,
    policy_id           INT            NULL,        -- NULL possible for FNOL before policy match
    handling_agent_id   INT            NULL,
    loss_date           DATE           NOT NULL,
    reported_date       DATE           NOT NULL,
    closed_date         DATE           NULL,
    claim_status        VARCHAR(10)    NOT NULL,    -- OPEN, CLOSED, DENIED, REOPENED
    loss_type           VARCHAR(20)    NOT NULL,    -- COLLISION, THEFT, FIRE, WATER, LIABILITY
    reserve_amount      DECIMAL(12,2)  NOT NULL,
    is_deleted          BIT            NOT NULL DEFAULT 0,
    updated_at          DATETIME2      NOT NULL
);

CREATE TABLE dbo.claim_payment (
    payment_id        INT            NOT NULL PRIMARY KEY,
    claim_id          INT            NOT NULL REFERENCES dbo.claim(claim_id),
    payment_date      DATE           NOT NULL,
    payment_amount    DECIMAL(12,2)  NOT NULL,
    payment_type      VARCHAR(10)    NOT NULL,      -- INDEMNITY, EXPENSE
    is_void           BIT            NOT NULL DEFAULT 0
);

/* Feed from billing system; the same txn_id can be re-sent with a newer load_ts */
CREATE TABLE dbo.premium_transaction (
    txn_id            BIGINT         NOT NULL,
    policy_id         INT            NOT NULL,
    txn_type          VARCHAR(10)    NOT NULL,      -- NEW, RENEW, ENDORSE, CANCEL
    txn_date          DATE           NOT NULL,
    written_premium   DECIMAL(12,2)  NOT NULL,      -- always positive in source
    fees              DECIMAL(10,2)  NULL,
    source_system     VARCHAR(10)    NOT NULL,
    load_ts           DATETIME2      NOT NULL
);

/* ----------------------------- TARGET -------------------------------- */
CREATE TABLE edw.dim_policyholder (
    policyholder_id   INT            NOT NULL PRIMARY KEY,
    full_name         NVARCHAR(101)  NOT NULL,
    email_normalized  NVARCHAR(100)  NULL,
    state_code        CHAR(2)        NOT NULL,
    age_band          VARCHAR(10)    NOT NULL,      -- UNKNOWN, <25, 25-44, 45-64, 65+
    dw_load_ts        DATETIME2      NOT NULL
);

CREATE TABLE edw.dim_agent (
    agent_key         INT IDENTITY   NOT NULL PRIMARY KEY,
    agent_id          INT            NOT NULL,
    agent_code        VARCHAR(10)    NOT NULL,
    region            VARCHAR(20)    NOT NULL
);

CREATE TABLE edw.dim_product (
    product_key       INT IDENTITY   NOT NULL PRIMARY KEY,
    product_id        INT            NOT NULL,
    product_code      VARCHAR(10)    NOT NULL,
    line_of_business  VARCHAR(20)    NOT NULL
);

CREATE TABLE edw.dim_policy (                     -- SCD Type 2
    policy_key          INT IDENTITY   NOT NULL PRIMARY KEY,
    policy_id           INT            NOT NULL,
    policy_number       VARCHAR(20)    NOT NULL,
    product_key         INT            NOT NULL,
    line_of_business    VARCHAR(20)    NOT NULL,
    status_code         CHAR(1)        NOT NULL,
    status_desc         VARCHAR(20)    NOT NULL,
    annual_premium      DECIMAL(12,2)  NOT NULL,
    deductible          DECIMAL(10,2)  NOT NULL,
    effective_date      DATE           NOT NULL,
    expiration_date     DATE           NOT NULL,
    row_effective_from  DATETIME2      NOT NULL,
    row_effective_to    DATETIME2      NOT NULL,     -- '9999-12-31' for current row
    is_current          BIT            NOT NULL
);

CREATE TABLE edw.fact_claims (
    claim_id              INT            NOT NULL PRIMARY KEY,
    claim_number          VARCHAR(20)    NOT NULL,
    policy_key            INT            NOT NULL,
    agent_key             INT            NOT NULL,   -- -1 = unknown agent
    loss_date_key         INT            NOT NULL,   -- yyyymmdd
    reported_date_key     INT            NOT NULL,
    claim_status          VARCHAR(10)    NOT NULL,
    loss_type             VARCHAR(20)    NOT NULL,
    days_to_report        INT            NOT NULL,
    days_open             INT            NULL,
    reserve_amount        DECIMAL(12,2)  NOT NULL,
    total_paid_indemnity  DECIMAL(14,2)  NOT NULL,
    total_paid_expense    DECIMAL(14,2)  NOT NULL,
    total_paid            DECIMAL(14,2)  NOT NULL,
    is_large_loss         CHAR(1)        NOT NULL,   -- Y/N
    dw_load_ts            DATETIME2      NOT NULL
);

CREATE TABLE edw.err_claims (
    claim_id          INT            NOT NULL,
    claim_number      VARCHAR(20)    NOT NULL,
    policy_id         INT            NULL,
    error_reason      VARCHAR(100)   NOT NULL,
    dw_load_ts        DATETIME2      NOT NULL
);

CREATE TABLE edw.fact_premium (
    txn_id            BIGINT         NOT NULL PRIMARY KEY,
    policy_key        INT            NOT NULL,
    txn_date_key      INT            NOT NULL,
    txn_type          VARCHAR(10)    NOT NULL,
    written_premium   DECIMAL(12,2)  NOT NULL,      -- signed: CANCEL is negative
    fees              DECIMAL(10,2)  NOT NULL,
    gross_premium     DECIMAL(12,2)  NOT NULL,
    dw_load_ts        DATETIME2      NOT NULL,
    dw_update_ts      DATETIME2      NULL
);

CREATE TABLE etl.watermark (
    job_name          VARCHAR(100)   NOT NULL PRIMARY KEY,
    last_value        DATETIME2      NOT NULL
);

CREATE TABLE etl.job_log (
    log_id            INT IDENTITY   NOT NULL PRIMARY KEY,
    job_name          VARCHAR(100)   NOT NULL,
    run_ts            DATETIME2      NOT NULL,
    rows_inserted     INT            NULL,
    rows_updated      INT            NULL,
    status            VARCHAR(10)    NOT NULL,
    error_message     NVARCHAR(4000) NULL
);

CREATE TABLE audit.claim_status_history (
    audit_id          INT IDENTITY   NOT NULL PRIMARY KEY,
    claim_id          INT            NOT NULL,
    old_status        VARCHAR(10)    NULL,
    new_status        VARCHAR(10)    NOT NULL,
    changed_at        DATETIME2      NOT NULL,
    changed_by        NVARCHAR(128)  NOT NULL
);
