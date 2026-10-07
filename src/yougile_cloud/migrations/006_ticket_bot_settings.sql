-- Ticket bot settings filled in on the admin page: the Telegram token (encrypted) and who
-- approves senders. One row. The tickets process re-reads it, so no restart is needed.
CREATE TABLE ticket_bot (
    id int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    token_enc bytea,
    bot_username text NOT NULL DEFAULT '',
    admins bigint[] NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- One bot account per YouGile company (the admin page replaces it in place).
CREATE UNIQUE INDEX ticket_accounts_company ON ticket_accounts (company_id);
