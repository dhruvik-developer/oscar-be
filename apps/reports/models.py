"""Reports and dashboards are queries, not tables (db.md 8, 12).

The only persisted artefact is the export job, which lives in apps.core.models
because it is platform plumbing rather than a reporting entity.
"""
