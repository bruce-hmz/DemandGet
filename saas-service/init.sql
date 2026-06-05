-- Create database if not exists
SELECT 'CREATE DATABASE chuhai'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'chuhai')\gexec

-- Connect to chuhai database
\c chuhai;

-- Create extension pgcrypto if not exists
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Create chuhai_user if not exists
DO
$$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'chuhai_user') THEN
      CREATE ROLE chuhai_user LOGIN PASSWORD '${POSTGRES_PASSWORD:-chuhai_pass}';
   END IF;
END
$$;

-- Grant privileges on database chuhai to chuhai_user
GRANT ALL PRIVILEGES ON DATABASE chuhai TO chuhai_user;

-- Grant schema usage and creation privileges
GRANT CREATE ON SCHEMA public TO chuhai_user;
GRANT USAGE ON SCHEMA public TO chuhai_user;

-- Enable row level security (RLS) - note: actual policies need to be defined per table
-- ALTER TABLE ... ENABLE ROW LEVEL SECURITY;
-- Example:
-- CREATE POLICY ... ON ... FOR ... USING (...);
