"""Schema55: bounded native text-input ownership, empty on upgrade."""

CLAUDE_CORRECTIONS_SCHEMA = """
CREATE TABLE claude_input_runs (
    parent_job_id TEXT PRIMARY KEY REFERENCES provider_jobs(job_id),
    initial_input_uuid TEXT NOT NULL UNIQUE,
    initial_digest TEXT NOT NULL CHECK(length(initial_digest)=64),
    native_session_id TEXT NOT NULL,
    project_root TEXT NOT NULL,
    initial_receipt_terminal INTEGER CHECK(initial_receipt_terminal=1),
    terminal_count INTEGER NOT NULL DEFAULT 0 CHECK(terminal_count BETWEEN 0 AND 2),
    completion_validated INTEGER NOT NULL DEFAULT 0 CHECK(completion_validated IN (0,1))
);
CREATE TABLE claude_input_children (
    parent_job_id TEXT PRIMARY KEY REFERENCES claude_input_runs(parent_job_id),
    child_job_id TEXT NOT NULL UNIQUE REFERENCES provider_jobs(job_id),
    child_lease_token TEXT NOT NULL,
    input_uuid TEXT NOT NULL UNIQUE,
    input_digest TEXT NOT NULL CHECK(length(input_digest)=64),
    receipt_terminal INTEGER CHECK(receipt_terminal IN (1,2)),
    CHECK(parent_job_id != child_job_id)
);
CREATE TRIGGER claude_input_run_no_replace BEFORE INSERT ON claude_input_runs
WHEN EXISTS(SELECT 1 FROM claude_input_runs WHERE parent_job_id=NEW.parent_job_id
 OR rowid=NEW.rowid OR initial_input_uuid=NEW.initial_input_uuid)
BEGIN SELECT RAISE(ABORT,'Claude input run cannot be replaced'); END;
CREATE TRIGGER claude_input_run_binding BEFORE UPDATE ON claude_input_runs
WHEN NEW.rowid IS NOT OLD.rowid OR NEW.parent_job_id IS NOT OLD.parent_job_id
 OR NEW.initial_input_uuid IS NOT OLD.initial_input_uuid
 OR NEW.initial_digest IS NOT OLD.initial_digest
 OR NEW.native_session_id IS NOT OLD.native_session_id
 OR NEW.project_root IS NOT OLD.project_root
 OR (OLD.initial_receipt_terminal IS NOT NULL AND NEW.initial_receipt_terminal IS NOT OLD.initial_receipt_terminal)
 OR NEW.terminal_count < OLD.terminal_count OR NEW.terminal_count > OLD.terminal_count+1
 OR NEW.completion_validated < OLD.completion_validated
BEGIN SELECT RAISE(ABORT,'Claude input run binding is immutable'); END;
CREATE TRIGGER claude_input_run_retained BEFORE DELETE ON claude_input_runs
BEGIN SELECT RAISE(ABORT,'Claude input run is retained'); END;
CREATE TRIGGER claude_input_child_no_replace BEFORE INSERT ON claude_input_children
WHEN EXISTS(SELECT 1 FROM claude_input_children WHERE parent_job_id=NEW.parent_job_id
 OR rowid=NEW.rowid OR child_job_id=NEW.child_job_id OR input_uuid=NEW.input_uuid)
BEGIN SELECT RAISE(ABORT,'Claude input child cannot be replaced'); END;
CREATE TRIGGER claude_input_child_binding BEFORE UPDATE ON claude_input_children
WHEN NEW.rowid IS NOT OLD.rowid OR NEW.parent_job_id IS NOT OLD.parent_job_id
 OR NEW.child_job_id IS NOT OLD.child_job_id OR NEW.child_lease_token IS NOT OLD.child_lease_token
 OR NEW.input_uuid IS NOT OLD.input_uuid OR NEW.input_digest IS NOT OLD.input_digest
 OR (OLD.receipt_terminal IS NOT NULL AND NEW.receipt_terminal IS NOT OLD.receipt_terminal)
BEGIN SELECT RAISE(ABORT,'Claude input child binding is immutable'); END;
CREATE TRIGGER claude_input_child_retained BEFORE DELETE ON claude_input_children
BEGIN SELECT RAISE(ABORT,'Claude input child is retained'); END;
"""
