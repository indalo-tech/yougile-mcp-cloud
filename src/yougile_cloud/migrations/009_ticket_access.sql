-- Managing who may send tickets after the first decision: an approver can take access away
-- («revoked»: the person may ask again, as someone new) and lift a block.
ALTER TABLE ticket_senders DROP CONSTRAINT ticket_senders_status_check;
ALTER TABLE ticket_senders ADD CONSTRAINT ticket_senders_status_check
    CHECK (status IN ('pending', 'approved', 'rejected', 'revoked', 'blocked'));

-- What the admin page asks of the bot: the page has no Telegram of its own to tell people (and
-- approval makes tickets of what they wrote meanwhile), so the bot's process carries it out.
CREATE TABLE ticket_actions (
    id bigserial PRIMARY KEY,
    tg_user_id bigint NOT NULL,
    action text NOT NULL,          -- approve, reject, move, revoke, block, unblock
    account_id int,                -- approve, move: where the tickets go from now on
    project_id text,
    user_id bigint,                -- the admin who asked (users.id)
    created_at timestamptz NOT NULL DEFAULT now(),
    done_at timestamptz,
    ok boolean,
    result text NOT NULL DEFAULT ''  -- the bot's one line about it, shown to the admin
);
CREATE INDEX ticket_actions_open ON ticket_actions (id) WHERE done_at IS NULL;
