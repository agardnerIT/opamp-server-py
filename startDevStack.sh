#!/usr/bin/env bash
# Local dev stack: FastAPI server + the OpAMP Console.
#
#   server   http://127.0.0.1:4320   FastAPI + OpAMP WebSocket
#   console  http://127.0.0.1:8501   the console (nginx serving the built web/dist,
#                                    /api proxied to the server service)
#
# 8501 is the product port (Streamlit's old one). Streamlit is gone — see
# new-ui-plans.md stage 10.
#
# Frontend edits need a rebuild here, because the container is a static bundle:
#   docker compose build ui && docker compose up -d ui
# For an edit/refresh loop use vite instead:
#   cd web && npm run dev            # 8501, /api -> 127.0.0.1:4320
# With this stack up, `ui` owns 8501, so vite takes 8502 (strictPort is false) —
# or stop `ui` first and vite gets the product port.
#
# Containerised HMR, opt-in, on 8502:
#   docker compose --profile console up console
export COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml
docker compose up --build &
wait $!
