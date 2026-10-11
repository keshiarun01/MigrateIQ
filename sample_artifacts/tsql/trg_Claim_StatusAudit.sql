/* =====================================================================
   Trigger   : dbo.trg_Claim_StatusAudit  (on OLTP table dbo.claim)
   Complexity: HIGH / NOT DIRECTLY PORTABLE
   Why it's here: Spark has no row-level triggers. The migration system must
   detect this, record it as an UnsupportedConstruct, and propose an
   equivalent pattern (CDC / Delta Change Data Feed / Iceberg changelog
   + a scheduled audit job) instead of generating fake "trigger" code.
   ===================================================================== */
CREATE OR ALTER TRIGGER dbo.trg_Claim_StatusAudit
ON dbo.claim
AFTER UPDATE
AS
BEGIN
    SET NOCOUNT ON;

    IF NOT UPDATE(claim_status)
        RETURN;

    /* Business rule BR-7: every status transition is audited */
    INSERT INTO audit.claim_status_history (claim_id, old_status, new_status, changed_at, changed_by)
    SELECT  i.claim_id,
            d.claim_status,
            i.claim_status,
            SYSDATETIME(),
            SUSER_SNAME()
    FROM    inserted i
    INNER JOIN deleted d
            ON d.claim_id = i.claim_id
    WHERE   ISNULL(d.claim_status, '') <> ISNULL(i.claim_status, '');

    /* Business rule BR-8: closing or denying a claim stamps closed_date;
       re-opening clears it. (Side effect on the source row itself.) */
    UPDATE  c
    SET     c.closed_date = CASE
                              WHEN i.claim_status IN ('CLOSED', 'DENIED')
                                   AND c.closed_date IS NULL THEN CAST(SYSDATETIME() AS DATE)
                              WHEN i.claim_status = 'REOPENED' THEN NULL
                              ELSE c.closed_date
                            END
    FROM    dbo.claim c
    INNER JOIN inserted i ON i.claim_id = c.claim_id
    INNER JOIN deleted  d ON d.claim_id = i.claim_id
    WHERE   d.claim_status <> i.claim_status;
END;
GO
