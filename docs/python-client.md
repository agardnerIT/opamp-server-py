# Python client library (`opamp_client`)

The CLI and MCP server both use one shared HTTP client — use it directly when
embedding OpAMP control into your own tooling:

```python
from client import OpampClient, OpampApiError

# Reads OPAMP_SERVER_URL / ADMIN_PASSWORD from the environment by default.
oc = OpampClient(base_url="http://localhost:4320", password="changeme")

agents = oc.list_agents(healthy="true", environment="prod")   # metadata filters
result = oc.generate_manifest(agents["agents"][0]["id"])
print(result["manifest_yaml"])

# SQL selection over the live fleet (#132)
print(oc.query_agents("SELECT * FROM agents WHERE host_arch = 'amd64'"))
print(oc.agent_query_schema())            # columns + observed values

# Remote config (#81): validate, push, inspect (redacted by default)
print(oc.validate_config({"": {"body": open("collector.yaml").read(), "content_type": "text/yaml"}}))
oc.set_agent_config(agent_id, {"": {"body": yaml_text, "content_type": "text/yaml"}})
print(oc.get_agent_config(agent_id)["desired"]["revision_id"])
oc.delete_agent_config(agent_id)          # withdraw
oc.apply_agent_config(config_map, agents=[agent_id], confirm=True)

# Config control plane (#99/#100): propose -> approve -> roll back
proposal = oc.propose_config(config_map, scope={"agent_id": agent_id}, note="why")
print(proposal["verdict"])                # {ok, errors, warnings, policy, fingerprint}
oc.approve_config(proposal["proposal_id"], note="reviewed")   # approver role
print(oc.get_agent_config(agent_id)["drift"])                 # is it running it?
oc.rollback_agent_config(agent_id, note="v2 broke tracing")
print(oc.list_config_revisions(status="poisoned"))            # metadata only

# Agent credentials (admin) — the token is returned once
token = oc.create_agent_token("4f3c2b1a...", label="prod-1")["token"]
print(oc.get_agent("4f3c2b1a...")["credential"])
print(oc.revoke_agent_token("4f3c2b1a..."))   # {"revoked": 1}

# Audit log (#97) — who did what
history = oc.audit_log(action="config.", limit=50)   # `action` is a prefix match
for entry in history["entries"]:
    print(entry["ts"], entry["actor"], entry["action"], entry["result"])
print(oc.audit_verify())          # {"ok": True, ...} — tamper check over the hash chain
print(oc.audit_export(format="jsonl"))   # raw text, oldest first

# Admin identity and sessions (#95)
session = oc.login("bob", "…")             # POST /auth/session -> token, role, expiry
scoped = OpampClient(base_url="http://localhost:4320", token=session["token"])
print(scoped.whoami())                     # {"name": "bob", "role": "operator", ...}
print(scoped.create_principal("carol", "approver", "…"))   # admin role only
scoped.logout()

try:
    oc.get_agent("nope")
except OpampApiError as e:
    print(e.status_code, e.detail)     # 404 Agent not found
except OpampConnectionError:
    print("server unreachable")
```

Structured errors: `OpampApiError(status_code, detail)` and
`OpampConnectionError`. Nothing is raised as a bare string.
