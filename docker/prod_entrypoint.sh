#!/bin/sh

if [ "$SEPARATE_HEALTH_APP" = "1" ]; then
    export LITELLM_ARGS="$@"
    export SUPERVISORD_STOPWAITSECS="${SUPERVISORD_STOPWAITSECS:-3600}"
    exec supervisord -c /etc/supervisord.conf
fi

# Internal-fork modification: invoke `python -m litellm_extras.entrypoint`
# instead of the upstream `litellm` console script. The wrapper installs
# PublicReqMiddleware on the FastAPI app before delegating to the original
# Click CLI, which is what enforces the X-Public-Req gating in production.
# Routing through the wrapper at the entrypoint level (rather than via
# docker-compose overrides) means every deployment of this image picks
# the middleware up automatically — there is no per-deploy step to forget.
if [ "$USE_DDTRACE" = "true" ]; then
    export DD_TRACE_OPENAI_ENABLED="False"
    exec ddtrace-run python -m litellm_extras.entrypoint "$@"
else
    exec python -m litellm_extras.entrypoint "$@"
fi