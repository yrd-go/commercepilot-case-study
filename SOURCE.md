# Source Review Guide

This is a sanitized public snapshot of the CommercePilot application. It is intended for
code review, not for exposing private data or production credentials.

## Architecture

The application is split into four layers:

1. `src/saviland/web/` owns the Starlette application, authentication boundary, API,
   storefront pages, and browser-side administration UI.
2. `src/saviland/domain/` contains pure business logic for catalog, orders, inventory,
   and advertising diagnosis.
3. `src/saviland/pipelines/` coordinates listing generation, compliance checks, weekly
   reporting, and HTML/Excel/Markdown exports.
4. `src/saviland/render/` produces deterministic charts and image variants with Pillow.

`src/saviland/rag_bridge.py` connects the listing pipeline to retrieval. Search hits retain
their source file and line metadata so generated copy can be traced back to evidence.

## Review paths

- Start with `src/saviland/web/server.py` and `web/api.py` for the application boundary.
- Read `web/auth.py` for the session model and explicit demo-level limitations.
- Read `pipelines/listing.py` for retrieval, structured generation, and compliance.
- Read `pipelines/weekly.py` for deterministic diagnosis before LLM attribution.
- Read `runs.py` for run archives, degradation state, and artifact records.
- Read `tests/test_saviland.py` for behavior-level regression cases.

## Data and security boundary

The public snapshot excludes runtime databases, scraped source files, product image
assets, logs, `.env` files, API keys, user records, and private working notes. The source
reads secrets from environment variables and never stores them in this repository.

Authentication is deliberately described as demo-level: it provides login and session
isolation, but not RBAC, password hashing, rate limiting, or production deployment
hardening. Those would belong to a real deployment layer.

## Reproduction note

Some demonstration and integration tests depend on private catalog fixtures and image
assets that are not published. The unit-level logic and module structure remain reviewable
without those files. There is no claim that a clean clone can reproduce every recorded
demo output without supplying the omitted local fixtures.
