# OpAMP Server Python

This in an [OpenTelemetry OpAMP](https://opentelemetry.io/docs/specs/opamp/) server and UI written in Python.

The server listens on the standard port of 4320.
The server offers the following endpoints:

* `/v1/opamp` = Agents (eg. OpenTelemetry collectors) are configured to send data to this endpoint
* `/` = The root path (eg. `http://127.0.0.1:4320/` offers an overview of the server and connected agents)
  
![image](https://github.com/user-attachments/assets/3b953c61-2b76-4173-b9ea-1e846f856a6c)

* `/agents` = Offers a deeper overview of all connected agents

![image](https://github.com/user-attachments/assets/2ee44967-deff-46d7-8247-b05db4a5226a)

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
