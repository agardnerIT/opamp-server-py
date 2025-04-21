# OpAMP Server Python

This in an OpenTelemetry OpAMP server and UI written in Python.

The server listens on the standard port of 4320.
The server offers the following endpoints:

* `/v1/opamp` = Agents (eg. OpenTelemetry collectors) are configured to send data to this endpoint
* `/` = The root path (eg. `http://127.0.0.1:4320/` offers an overview of the server and connected agents)
* `/agents` = Offers a deeper overview of all connected agents
* `/agent/<agent-id>` = Offers a full overview of a single connected agent


## Run it

```py
pip install -r requirements.txt
fastapi run server.py --host 127.0.0.1 --port 4320
```

## Sample Collector Config
An agent (eg. collector) needs to be configured to connect to the server. [A sample configuration file is provided](https://github.com/agardnerIT/opamp-server-py/blob/main/collector/config.yaml).
