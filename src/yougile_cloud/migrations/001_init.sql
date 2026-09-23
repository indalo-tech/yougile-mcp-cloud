-- Tenants are YouGile companies; users are YouGile accounts inside them.

CREATE TABLE companies (
    id               text PRIMARY KEY,                -- YouGile company id
    name             text NOT NULL,
    status           text NOT NULL DEFAULT 'trial'
                     CHECK (status IN ('trial', 'active', 'exempt', 'blocked')),
    trial_ends_at    timestamptz,
    paid_until       timestamptz,
    settings         jsonb NOT NULL DEFAULT '{}'::jsonb,  -- timezone, instructions, workflows, ...
    settings_version integer NOT NULL DEFAULT 1,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id      text NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    yougile_user_id text NOT NULL,
    email           text NOT NULL DEFAULT '',
    name            text NOT NULL DEFAULT '',
    is_admin        boolean NOT NULL DEFAULT false,   -- admin of the YouGile company
    api_key_enc     bytea NOT NULL,                   -- Fernet-encrypted YouGile API key
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz,
    UNIQUE (company_id, yougile_user_id)
);

-- MCP permissions set by the company admin; they only narrow YouGile's own rights.
CREATE TABLE user_rights (
    company_id      text NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    yougile_user_id text NOT NULL,
    role            text CHECK (role IN ('reader', 'member', 'admin')),
    projects        jsonb,                            -- null: all projects
    deny            jsonb NOT NULL DEFAULT '[]'::jsonb,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, yougile_user_id)
);

CREATE TABLE oauth_clients (
    client_id  text PRIMARY KEY,
    info       jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE refresh_tokens (
    token_hash text PRIMARY KEY,                      -- sha256 of the opaque token
    user_id    bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    client_id  text NOT NULL,
    scopes     jsonb NOT NULL DEFAULT '[]'::jsonb,
    resource   text,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX refresh_tokens_user ON refresh_tokens (user_id);

CREATE TABLE audit_log (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at         timestamptz NOT NULL DEFAULT now(),
    company_id text,
    user_id    bigint,
    event      text NOT NULL,
    detail     jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX audit_log_company_at ON audit_log (company_id, at DESC);
