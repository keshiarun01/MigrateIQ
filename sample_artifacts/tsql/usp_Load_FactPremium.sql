/* =====================================================================
   Procedure : etl.usp_Load_FactPremium
   Complexity: MEDIUM-HIGH
   Pattern   : watermark-based incremental load, dedupe of re-sent rows,
               SCD2 point-in-time dimension join, MERGE upsert, job logging.
   ===================================================================== */
CREATE OR ALTER PROCEDURE etl.usp_Load_FactPremium
    @RunTs DATETIME2 = NULL
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @JobName   VARCHAR(100) = 'usp_Load_FactPremium';
    DECLARE @LastValue DATETIME2;
    DECLARE @NewValue  DATETIME2;
    DECLARE @Inserted  INT = 0, @Updated INT = 0;
    DECLARE @MergeOut  TABLE (action_type NVARCHAR(10));

    SET @RunTs = ISNULL(@RunTs, SYSDATETIME());

    BEGIN TRY
        SELECT @LastValue = last_value
        FROM etl.watermark
        WHERE job_name = @JobName;

        SET @LastValue = ISNULL(@LastValue, '1900-01-01');

        /* 1. Pull changed rows; keep only the latest copy of each txn_id */
        ;WITH changed AS (
            SELECT  t.txn_id, t.policy_id, t.txn_type, t.txn_date,
                    t.written_premium, t.fees, t.load_ts,
                    ROW_NUMBER() OVER (PARTITION BY t.txn_id
                                       ORDER BY t.load_ts DESC) AS rn
            FROM    dbo.premium_transaction t
            WHERE   t.load_ts > @LastValue
              AND   t.source_system IN ('BILLING', 'LEGACY_BILL')
        )
        SELECT  c.txn_id,
                d.policy_key,
                CONVERT(INT, FORMAT(c.txn_date, 'yyyyMMdd'))              AS txn_date_key,
                UPPER(c.txn_type)                                          AS txn_type,
                CASE WHEN c.txn_type = 'CANCEL'
                     THEN -1 * c.written_premium
                     ELSE c.written_premium END                            AS written_premium,
                ISNULL(c.fees, 0)                                          AS fees,
                CASE WHEN c.txn_type = 'CANCEL'
                     THEN -1 * c.written_premium
                     ELSE c.written_premium END + ISNULL(c.fees, 0)        AS gross_premium,
                c.load_ts
        INTO    #stg_premium
        FROM    changed c
        /* point-in-time join: the policy version valid on the transaction date */
        INNER JOIN edw.dim_policy d
                ON  d.policy_id = c.policy_id
                AND CAST(c.txn_date AS DATETIME2) >= d.row_effective_from
                AND CAST(c.txn_date AS DATETIME2) <  d.row_effective_to
        WHERE   c.rn = 1;

        SELECT @NewValue = MAX(load_ts) FROM #stg_premium;

        /* 2. Upsert */
        MERGE edw.fact_premium AS tgt
        USING #stg_premium      AS src
           ON tgt.txn_id = src.txn_id
        WHEN MATCHED AND (   tgt.written_premium <> src.written_premium
                          OR tgt.fees            <> src.fees
                          OR tgt.txn_type        <> src.txn_type
                          OR tgt.policy_key      <> src.policy_key) THEN
            UPDATE SET tgt.policy_key      = src.policy_key,
                       tgt.txn_date_key    = src.txn_date_key,
                       tgt.txn_type        = src.txn_type,
                       tgt.written_premium = src.written_premium,
                       tgt.fees            = src.fees,
                       tgt.gross_premium   = src.gross_premium,
                       tgt.dw_update_ts    = @RunTs
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (txn_id, policy_key, txn_date_key, txn_type, written_premium,
                    fees, gross_premium, dw_load_ts, dw_update_ts)
            VALUES (src.txn_id, src.policy_key, src.txn_date_key, src.txn_type,
                    src.written_premium, src.fees, src.gross_premium, @RunTs, NULL)
        OUTPUT $action INTO @MergeOut;

        SELECT @Inserted = SUM(CASE WHEN action_type = 'INSERT' THEN 1 ELSE 0 END),
               @Updated  = SUM(CASE WHEN action_type = 'UPDATE' THEN 1 ELSE 0 END)
        FROM   @MergeOut;

        /* 3. Advance watermark only if rows were processed */
        IF @NewValue IS NOT NULL
        BEGIN
            UPDATE etl.watermark SET last_value = @NewValue WHERE job_name = @JobName;
            IF @@ROWCOUNT = 0
                INSERT INTO etl.watermark (job_name, last_value) VALUES (@JobName, @NewValue);
        END

        INSERT INTO etl.job_log (job_name, run_ts, rows_inserted, rows_updated, status)
        VALUES (@JobName, @RunTs, ISNULL(@Inserted, 0), ISNULL(@Updated, 0), 'SUCCESS');

        DROP TABLE IF EXISTS #stg_premium;
    END TRY
    BEGIN CATCH
        INSERT INTO etl.job_log (job_name, run_ts, status, error_message)
        VALUES (@JobName, @RunTs, 'FAILED', ERROR_MESSAGE());
        THROW;
    END CATCH
END;
GO
