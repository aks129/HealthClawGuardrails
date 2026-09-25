# Local-only values for the demo stack. Copy to env.sh, fill in, and source it
# before the capture scripts. Every secret here is a local placeholder: the
# stack runs on 127.0.0.1 against SQLite files under $V/run.
V=/path/to/your/video-scratch-dir        # the copied scripts/ directory
WT=/path/to/a/checkout/of/healthclaw     # the commit you are filming
export APP_ENV=development FLASK_ENV=development
export SQLALCHEMY_DATABASE_URI=sqlite:///$V/run/engine.db
export INTERNAL_TOKEN_MINT_SECRET=CHANGE-ME STEP_UP_SECRET=CHANGE-ME-at-least-32-characters
export PUBLIC_TENANTS=desktop-demo PUBLIC_BASE_URL=http://127.0.0.1:5099
export CARE_ENV=development CARE_DATABASE_URL=sqlite:///$V/run/careagents.db
export CARE_SESSION_SECRET=CHANGE-ME-at-least-32-characters
export HEALTHCLAW_BASE=http://127.0.0.1:5099 HEALTHCLAW_PUBLIC_BASE=http://127.0.0.1:5099
# HEALTHCLAW_MINT_SECRET must equal INTERNAL_TOKEN_MINT_SECRET above.
export HEALTHCLAW_MINT_SECRET=CHANGE-ME CARE_ORIGIN=http://127.0.0.1:8600 CARE_RP_ID=127.0.0.1
export RESEND_API_KEY= TELEGRAM_BOT_TOKEN= CARE_REAL_RECORDS= CARE_REAL_RECORDS_ALLOWLIST=
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
