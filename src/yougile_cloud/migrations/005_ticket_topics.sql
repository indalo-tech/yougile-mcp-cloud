-- Ticket bot: each ticket may have its own topic in the sender's private chat with the bot.
-- NULL: no topic (topics off for the bot, or Telegram refused one): the main chat is used.
ALTER TABLE tickets ADD COLUMN tg_thread_id bigint;
CREATE INDEX tickets_thread ON tickets (tg_user_id, tg_thread_id) WHERE tg_thread_id IS NOT NULL;
