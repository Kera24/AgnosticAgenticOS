"""Agentic OS Control Centre — local dashboard service.

A thin, loopback-only HTTP layer over the existing orchestration modules.
No orchestration logic lives here; the Python core remains the source of
truth. Nothing in this package reads credential files, executes arbitrary
commands, or pushes/merges/deploys anything.
"""
