# OpAMP Server Python

This in an [OpenTelemetry OpAMP](https://opentelemetry.io/docs/specs/opamp/) server and UI written in Python.

The server listens on the standard port of 4320.
The server offers the following endpoints:

* `/v1/opamp` = Agents (eg. OpenTelemetry collectors) are configured to send data to this endpoint
* `/` = The root path (eg. `http://127.0.0.1:4320/` offers an overview of the server and connected agents)
  
![image](https://github.com/user-attachments/assets/d76d1d89-1632-41be-90df-c99a2120f4aa)


* `/agents` = Offers a deeper overview of all connected agents

![image](https://github.com/user-attachments/assets/49ae48c4-eb97-45bd-b6e9-95cc4c413488)


* `/agent/<agent-id>` = Offers a full overview of a single connected agent

It is important to note that OpAMP is an open protocol for agents to connect to, and be managed by, servers. Any software can thus act as an agent and be managed at scale using OpAMP.
Hopefully, OpAMP offers a new vendor-neutral way to perform software upgrades, maintenance and patching.

- Antivirus / security agents
- Observability agents
- Operating systems
- Any other software that runs and requires periodic updates or configuration changes

## Run it

```py
pip install -r requirements.txt
fastapi run server.py --host 127.0.0.1 --port 4320
```

## Sample Collector Config
An agent (eg. collector) needs to be configured to connect to the server. [A sample configuration file is provided](https://github.com/agardnerIT/opamp-server-py/blob/main/collector/config.yaml).

```
./otelcol-contrib --config=config.yaml
```
