-- Ticket bot in topic mode: there is no main chat, every conversation is a topic.
-- Drafts are per topic (thread 0: the main chat, when topics are off), a sender remembers the
-- topic they asked for access in (answers about access go there), and each approver gets one
-- topic for access requests.
ALTER TABLE ticket_drafts ADD COLUMN thread bigint NOT NULL DEFAULT 0;
ALTER TABLE ticket_drafts DROP CONSTRAINT ticket_drafts_pkey;
ALTER TABLE ticket_drafts ADD PRIMARY KEY (tg_user_id, thread);

ALTER TABLE ticket_senders ADD COLUMN tg_thread_id bigint;

CREATE TABLE ticket_admin_topics (
    tg_user_id bigint PRIMARY KEY,
    thread bigint NOT NULL
);
