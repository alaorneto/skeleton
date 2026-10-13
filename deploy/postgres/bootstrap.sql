-- Run as an authorized database operator against a NEW selected database.
-- Passwords are set separately with psql \password, never committed here.
-- This script does not create databases or overwrite existing data.
DO $roles$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='skeleton_owner') THEN
        CREATE ROLE skeleton_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='skeleton_runtime') THEN
        CREATE ROLE skeleton_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='skeleton_audit_writer') THEN
        CREATE ROLE skeleton_audit_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
END
$roles$;
CREATE SCHEMA IF NOT EXISTS skeleton AUTHORIZATION skeleton_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA skeleton FROM PUBLIC;
GRANT USAGE ON SCHEMA skeleton TO skeleton_runtime, skeleton_audit_writer;
-- Reviewed migrations transfer SECURITY DEFINER functions to the audit writer.
-- Runtime must NEVER be a member of either privileged role.
GRANT skeleton_audit_writer TO skeleton_owner;
ALTER ROLE skeleton_owner SET search_path TO skeleton, public;
ALTER ROLE skeleton_runtime SET search_path TO skeleton, public;
-- All table grants/policies are installed by reviewed migrations, not blanket DML.
