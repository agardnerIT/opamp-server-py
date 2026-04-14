import json
import os
import requests
from typing import Optional, Dict, Any, List
from loguru import logger


OPA_URL = os.environ.get("OPA_URL", "http://localhost:8181")
OPA_ENABLED = os.environ.get("OPA_ENABLED", "false").lower() in ("true", "1", "yes")


class OPAClient:
    def __init__(self, base_url: str = OPA_URL):
        self.base_url = base_url.rstrip("/")
        self.timeout = 5

    def is_available(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/health", timeout=self.timeout)
            return resp.status_code == 200
        except Exception:
            return False

    def get_policies(self) -> List[Dict[str, str]]:
        try:
            resp = requests.get(f"{self.base_url}/v1/policies", timeout=self.timeout)
            if resp.status_code != 200:
                return []
            
            policies = resp.json().get("result", [])
            result = []
            for policy in policies:
                policy_id = policy.get("id", "") if isinstance(policy, dict) else policy
                if not policy_id.endswith(".rego"):
                    continue
                pkg_name = policy_id.split("/")[-1].replace(".rego", "")
                if not pkg_name.startswith("require_"):
                    continue
                rule_name = pkg_name.replace("require_", "")
                raw = policy.get("raw", "") if isinstance(policy, dict) else ""
                violations = self._extract_violation_messages(raw)
                result.append({
                    "name": rule_name,
                    "description": violations[0] if violations else "No description",
                    "raw": raw,
                })
            return result
        except Exception as e:
            logger.warning(f"Failed to get policies: {e}")
            return []

    def _extract_violation_messages(self, raw: str) -> List[str]:
        messages = []
        for line in raw.split("\n"):
            if 'msg := "' in line:
                msg = line.split('msg := "')[1].split('"')[0]
                messages.append(msg)
        return messages

    def evaluate(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            resp = requests.get(
                f"{self.base_url}/v1/policies",
                timeout=self.timeout
            )
            
            if resp.status_code != 200:
                return {"compliant": None, "violations": [], "error": f"Policies fetch failed: {resp.status_code}"}
            
            policies = resp.json().get("result", [])
            
            all_violations = []
            policy_results = []
            
            for policy in policies:
                if isinstance(policy, str):
                    policy_id = policy
                else:
                    policy_id = policy.get("id", "")
                if not policy_id.endswith(".rego"):
                    continue
                
                pkg_name = policy_id.split("/")[-1].replace(".rego", "")
                if not pkg_name.startswith("require_"):
                    continue
                
                rule_name = pkg_name.replace("require_", "")
                pkg = f"opamp/agent/compliance/{rule_name}"
                
                resp = requests.post(
                    f"{self.base_url}/v1/data/{pkg}",
                    json={"input": input_data},
                    timeout=self.timeout
                )
                
                if resp.status_code == 200:
                    result = resp.json()
                    violations = result.get("result", {}).get("violations", [])
                    policy_results.append({
                        "name": rule_name,
                        "status": "fail" if violations else "pass",
                        "violations": violations,
                    })
                    all_violations.extend(violations)
                elif resp.status_code == 404:
                    policy_results.append({
                        "name": rule_name,
                        "status": "unknown",
                        "violations": [],
                    })
            
            return {
                "compliant": len(all_violations) == 0,
                "violations": all_violations,
                "policy_results": policy_results,
            }
        except Exception as e:
            logger.warning(f"OPA evaluation error: {e}")
            return {"compliant": None, "violations": [], "error": str(e)}

    def reload(self) -> bool:
        """Trigger OPA to reload bundles"""
        try:
            resp = requests.post(
                f"{self.base_url}/v1/bundles",
                timeout=self.timeout
            )
            return resp.status_code in (200, 201, 204)
        except Exception as e:
            logger.warning(f"OPA reload error: {e}")
            return False


_client: Optional[OPAClient] = None


def get_opa_client() -> Optional[OPAClient]:
    global _client
    
    if not OPA_ENABLED:
        return None
    
    if _client is None:
        _client = OPAClient()
        
        if not _client.is_available():
            logger.warning(f"OPA server not available at {OPA_URL}")
            _client = None
            return None
        
        logger.info("OPA server ready")
    
    return _client


def get_available_policies() -> List[Dict[str, str]]:
    client = get_opa_client()
    if client is None:
        return []
    return client.get_policies()


def reload_policies() -> bool:
    """Trigger OPA to reload policies"""
    client = get_opa_client()
    if client is None:
        return False
    return client.reload()


def validate_policy_file(filepath: str) -> Dict[str, Any]:
    """Validate a single policy file and return metadata"""
    import re
    
    result = {
        "filename": os.path.basename(filepath),
        "valid": False,
        "errors": [],
        "warnings": [],
        "name": None,
        "description": None,
    }
    
    try:
        with open(filepath, 'r') as f:
            content = f.read()
    except Exception as e:
        result["errors"].append(f"Cannot read file: {e}")
        return result
    
    # Check for package declaration
    pkg_match = re.search(r'^\s*package\s+opamp\.agent\.compliance\.([^\s]+)', content, re.MULTILINE)
    if not pkg_match:
        result["errors"].append("Missing package declaration: package opamp.agent.compliance.<name>")
        return result
    
    result["name"] = pkg_match.group(1)
    
    # Check for violations rule
    if "violations" not in content:
        result["errors"].append("Missing 'violations' rule")
        return result
    
    # Extract violation messages
    msg_matches = re.findall(r'msg\s*:=\s*"([^"]+)"', content)
    if msg_matches:
        result["description"] = msg_matches[0]
    
    # Check filename convention
    filename = os.path.basename(filepath)
    expected_prefix = f"require_{result['name']}.rego"
    if filename != expected_prefix and not filename.startswith("require_"):
        result["warnings"].append(f"Filename should be: {expected_prefix}")
    
    result["valid"] = len(result["errors"]) == 0
    return result


def get_policy_validation() -> List[Dict[str, Any]]:
    """Scan policies directory and validate all .rego files"""
    import glob
    
    policies_dir = os.environ.get("POLICIES_DIR", "policies/tags")
    results = []
    
    # Find all .rego files
    pattern = os.path.join(policies_dir, "**", "*.rego")
    rego_files = glob.glob(pattern, recursive=True)
    
    for filepath in rego_files:
        result = validate_policy_file(filepath)
        result["path"] = filepath
        results.append(result)
    
    return results


def evaluate_agent_compliance(agent_state) -> Dict[str, Any]:
    client = get_opa_client()
    
    if client is None:
        return {"compliant": None, "violations": [], "opa_disabled": True}
    
    input_data = {
        "agent_id": agent_state.agent_id,
        "description": agent_state.description,
    }
    
    result = client.evaluate(input_data)
    result["evaluated_at"] = None
    
    return result
