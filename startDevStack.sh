export COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml
docker compose up --build &

PID=$!
sleep 1
# Where things land (2027-ui Stage B):
#   server   http://127.0.0.1:4320        FastAPI + OpAMP WebSocket
#   ui       http://127.0.0.1:8501        Streamlit UI (until stage-10 cut-over)
#   console  http://127.0.0.1:8501        2027 web console — opt-in, see below
# The console and the Streamlit ui share port 8501, so they cannot run at the
# same time: `docker compose stop ui`, then
#   docker compose --profile console up console
# The dev proxy target is the compose `server` service; the API stays on 4320.
wait $PID
