import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { listAgents } from "../api/endpoints";
import {
  checkAgentCompliance,
  getPolicyValidation,
  reloadPolicies,
  type ComplianceCheckResult,
  type PolicyValidation,
} from "../api/admin";
import { useSession } from "../api/session";
import { roleAtLeast } from "../lib/roles";
import { EmptyState } from "../components/EmptyState";
import { Section } from "../components/Section";
import { PageHeader } from "../components/ui/PageHeader";
import { Button } from "../components/ui/Button";

/**
 * Compliance (new-ui stage 8): the OPA policy files this server loads, and a
 * per-agent check. The fleet-wide verdicts already live on the Agents page
 * (the `compliance` block every list row carries), so this page keeps only
 * what that view cannot show: policy validation, the admin reload, and an
 * on-demand re-check of one agent.
 *
 * One agent per check (a select, not Streamlit's multiselect): the result
 * renders under the button, and a batch would need a results table this page
 * does not have. A whole-fleet re-check is `/config/rollouts` territory.
 */

function PolicyRow({ policy }: { policy: PolicyValidation }) {
  return (
    <li className="flex flex-col gap-0.5">
      <div className="flex items-baseline gap-2">
        <span aria-hidden className="font-mono text-sm">
          {policy.valid ? "✓" : "✗"}
        </span>
        <span className="text-sm font-medium">{policy.filename}</span>
        {policy.name && <span className="text-xs text-[var(--color-muted)]">{policy.name}</span>}
      </div>
      {policy.description && (
        <span className="text-xs text-[var(--color-muted)]">{policy.description}</span>
      )}
      {policy.errors.map((err) => (
        <span key={err} className="text-xs text-[var(--color-danger)]">
          {err}
        </span>
      ))}
    </li>
  );
}

export default function Compliance() {
  const session = useSession();
  const canAdmin = roleAtLeast(session.data?.role, "admin");

  const policies = useQuery({ queryKey: ["compliance-validate"], queryFn: getPolicyValidation });
  const agents = useQuery({ queryKey: ["agents", {}], queryFn: () => listAgents() });

  const [agentId, setAgentId] = useState("");
  const [check, setCheck] = useState<ComplianceCheckResult | null>(null);

  const reload = useMutation({
    mutationFn: reloadPolicies,
    // Reload changes what later verdicts mean: refetch the validation list.
    onSuccess: () => void policies.refetch(),
  });

  const runCheck = useMutation({
    mutationFn: () => {
      setCheck(null);
      return checkAgentCompliance(agentId);
    },
    onSuccess: (result) => setCheck(result),
  });

  const valid = policies.data?.policies.filter((p) => p.valid) ?? [];
  const invalid = policies.data?.policies.filter((p) => !p.valid) ?? [];

  return (
    <section className="flex flex-col gap-4">
      <PageHeader
        title="Compliance"
        lede="The OPA policy files this server loads, and per-agent checks. Fleet-wide verdicts are on each agent's row in the Agents list."
      />

      {policies.data && !policies.data.opa_enabled && (
        <EmptyState
          title="OPA is not enabled"
          detail="This server was started without an OPA connection, so there are no policies to validate and no checks to run."
        />
      )}

      {policies.data?.opa_enabled && (
        <Section
          title="Policies"
          caption={
            `${valid.length} valid, ${invalid.length} invalid — ` +
            "loaded from the server's POLICIES_DIR"
          }
          actions={
            <Button
              size="sm"
              onClick={() => reload.mutate()}
              disabled={!canAdmin || reload.isPending}
              title={canAdmin ? "Ask OPA to reload policies from disk" : "Reloading policies needs the admin role"}
            >
              Reload & re-validate
            </Button>
          }
        >
          {reload.isSuccess && reload.data.success === false && (
            <p role="alert" className="mb-2 text-xs text-[var(--color-danger)]">
              Reload failed: {reload.data.error}
            </p>
          )}
          {(valid.length > 0 || invalid.length > 0) && (
            <ul className="flex flex-col gap-2">
              {[...valid, ...invalid].map((p) => (
                <PolicyRow key={p.filename} policy={p} />
              ))}
            </ul>
          )}
          {valid.length === 0 && invalid.length === 0 && (
            <p className="text-xs text-[var(--color-muted)]">No .rego files found.</p>
          )}
        </Section>
      )}

      <Section
        title="Check an agent"
        caption="Runs the policies against one agent now and records the verdict on it."
      >
        <div className="flex flex-wrap items-center gap-2">
          <select
            aria-label="Select an agent"
            value={agentId}
            onChange={(e) => {
              setAgentId(e.target.value);
              setCheck(null);
            }}
            className="field"
          >
            <option value="">Select an agent…</option>
            {(agents.data?.agents ?? []).map((a) => (
              <option key={a.id} value={a.id}>
                {a.name ?? `${a.id.slice(0, 12)}…`}
              </option>
            ))}
          </select>
          <Button
            variant="primary"
            onClick={() => runCheck.mutate()}
            disabled={!agentId || runCheck.isPending}
          >
            Check compliance
          </Button>
        </div>
        {check && (
          <p role="status" className="mt-3 text-sm">
            {check.error ? (
              <span className="text-[var(--color-danger)]">{check.error}</span>
            ) : check.compliant ? (
              <span className="text-[var(--color-ok)]">✓ Compliant</span>
            ) : (
              <span className="text-[var(--color-danger)]">
                ✗ {check.violations?.length ?? 0} violation(s)
                {check.violations?.length
                  ? `: ${check.violations.join("; ")}`
                  : ""}
              </span>
            )}
          </p>
        )}
      </Section>
    </section>
  );
}
