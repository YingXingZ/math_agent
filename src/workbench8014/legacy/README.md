# 8014 historical snapshots

This directory contains pre-consolidation `api_app` snapshots retained only for
forensics and migration comparison. They are not imported by the 8014
launcher, deployment scripts, or regression suite.

The only supported production module is `../api_app.py`. Do not apply fixes to
these snapshots; add tests or make changes against the canonical module.
