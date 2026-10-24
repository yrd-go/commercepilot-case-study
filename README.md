# CommercePilot

Independent, non-commercial portfolio case study for an AI-assisted commerce workflow.

**Live portfolio:** https://yrd-go.github.io/commercepilot-case-study/

This repository contains the public portfolio, the recorded walkthrough, and a sanitized
source-review copy of the application. It does not contain runtime data, product assets,
databases, user records, credentials, environment files, logs, or private working files.

## Scope

- Buyer storefront and administration UI walkthrough
- AI listing generation with retrieval, compliance rules, and rendered assets
- Advertising-week diagnosis, charts, and export formats
- Run archive with model, duration, degradation, and artifact traceability
- Source review copy in `src/`, `tests/`, `run.py`, and `requirements.txt`

## Source layout

- `src/saviland/web/`: Starlette entry points, authentication boundary, API, and storefront UI
- `src/saviland/domain/`: catalog, orders, inventory, and advertising calculations
- `src/saviland/pipelines/`: listing generation, compliance rules, weekly diagnosis, and exports
- `src/saviland/render/`: deterministic chart and hero-image rendering
- `src/saviland/rag_bridge.py`: retrieval integration and source traceability
- `tests/`: offline regression tests; some integration cases require local fixtures that are not public

The source snapshot is provided for code review. It is not a turnkey deployment package:
the private catalog fixtures, product assets, and model credentials are intentionally
omitted. Set `SAVILAND_BASE_AGENT` and model keys only in your own local environment.

See [SOURCE.md](SOURCE.md) for the architecture and review path.

Orders and advertising figures are structurally realistic simulated data. Product imagery
is used only for non-commercial portfolio display. No customer, account, or credential
records are included.

## Disclaimer

This is an independent project. It is not affiliated with, endorsed by, or sponsored by
Saviland. Saviland and related trademarks belong to their respective owners. All product
imagery is used solely for illustrative, non-commercial portfolio purposes.

Copyright (c) 2026. Portfolio presentation only.
