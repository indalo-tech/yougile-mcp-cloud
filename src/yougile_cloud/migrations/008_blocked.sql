-- A company admin may forbid a person to connect at all (not only disconnect them once).
-- Kept in the rights row; resetting the rights to the company defaults keeps it.
ALTER TABLE user_rights ADD COLUMN blocked boolean NOT NULL DEFAULT false;
