-- Ticket bot, many customers: an approver binds each sender to a project (the customer), and
-- the sender's tickets land on that project's «Заявки» board. What was a customer becomes the
-- bot's account in a YouGile company. Nothing was stored in these tables before this change.

ALTER TABLE ticket_customers RENAME TO ticket_accounts;
ALTER TABLE ticket_accounts DROP COLUMN project_id, DROP COLUMN column_id;

ALTER TABLE ticket_senders RENAME COLUMN customer_id TO account_id;
ALTER TABLE ticket_senders
    ADD COLUMN project_id text,
    ADD COLUMN project_name text NOT NULL DEFAULT '';

ALTER TABLE tickets RENAME COLUMN customer_id TO account_id;
ALTER TABLE tickets ADD COLUMN project_id text NOT NULL DEFAULT '';
ALTER TABLE tickets ALTER COLUMN project_id DROP DEFAULT;
