-- Telegram ticket bot: customers' staff send requests that become YouGile tasks, and hear back
-- about those tasks' columns and chat. Message texts live in YouGile, not here (drafts aside).

-- A customer whose staff send tickets, and where their tickets go.
CREATE TABLE ticket_customers (
    id serial PRIMARY KEY,
    name text NOT NULL UNIQUE,
    company_id text NOT NULL,      -- the YouGile company (and rate-limit bucket)
    project_id text NOT NULL,      -- the client-facing project: nothing outside it is relayed
    column_id text NOT NULL,       -- new tickets land here
    bot_user_id text NOT NULL,     -- the YouGile account the bot writes as
    api_key_enc bytea NOT NULL,    -- that account's key, encrypted with ENCRYPTION_KEYS
    created_at timestamptz NOT NULL DEFAULT now()
);

-- A Telegram user who asked for access; an approver binds them to a customer.
CREATE TABLE ticket_senders (
    tg_user_id bigint PRIMARY KEY,
    customer_id int REFERENCES ticket_customers (id),
    name text NOT NULL DEFAULT '',
    username text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'blocked')),
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz,
    decided_by bigint
);

-- A task the bot created. Only these tasks are ever reported back to Telegram.
CREATE TABLE tickets (
    task_id text PRIMARY KEY,
    customer_id int NOT NULL REFERENCES ticket_customers (id),
    tg_user_id bigint NOT NULL REFERENCES ticket_senders (tg_user_id),
    number text NOT NULL,
    title text NOT NULL,
    column_id text,
    completed boolean NOT NULL DEFAULT false,
    deleted boolean NOT NULL DEFAULT false,
    last_message_id bigint NOT NULL DEFAULT 0,  -- newest chat message seen (ids are timestamps)
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tickets_open ON tickets (created_at) WHERE NOT completed AND NOT deleted;
CREATE INDEX tickets_sender ON tickets (tg_user_id, created_at);

-- Telegram messages about a ticket: a reply to one of them goes to that ticket's chat.
CREATE TABLE ticket_tg_messages (
    tg_chat_id bigint NOT NULL,
    tg_message_id bigint NOT NULL,
    task_id text NOT NULL REFERENCES tickets (task_id) ON DELETE CASCADE,
    PRIMARY KEY (tg_chat_id, tg_message_id)
);

-- A conversation in progress (the ticket being written); removed once sent or cancelled.
CREATE TABLE ticket_drafts (
    tg_user_id bigint PRIMARY KEY,
    state jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
