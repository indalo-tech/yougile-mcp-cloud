-- The board a person chose with yougile_use_board (a YouGile board id); null: none chosen.
ALTER TABLE users ADD COLUMN default_board text;
