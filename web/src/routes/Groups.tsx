import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { attrValue, getQuerySchema, listAgents } from "../api/endpoints";
import {
  createGroup,
  deleteGroup,
  listGroups,
  previewGroup,
  updateGroup,
  type GroupPreview,
  type GroupView,
} from "../api/groups";
import { ApiError } from "../api/errors";
import { useSession } from "../api/session";
import { roleAtLeast } from "../lib/roles";
import { EmptyState } from "../components/EmptyState";
import { Modal } from "../components/Modal";
import { Section } from "../components/Section";
import { SqlSearch } from "../components/SqlSearch";
import { StatusBadge } from "../components/StatusBadge";
import { AgentTypeBadge } from "../components/AgentTypeBadge";
import { getAgentTypes } from "../api/endpoints";

/**
 * Groups (new-ui stage 7): saved SQL selections, not member lists. The page is
 * deliberately read-mostly and follows `ui/pages/3_Groups.py`: search first,
 * name the query second. Membership is resolved on read server-side, so every
 * count here is from the last fetch, never cached truth.
 *
 * Running a search and previewing a group are viewer actions; saving, editing
 * and deleting call operator endpoints — gated here as hints, the server
 * re-authorises every one.
 */

const DEFAULT_SQL = "SELECT * FROM agents WHERE healthy = 1";

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

function formatWhen(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** Pins text ("ids, comma separated") ⇄ list. */
function pinList(text: string): string[] {
  return text.split(",").map((s) => s.trim()).filter(Boolean);
}

export default function Groups() {
  const queryClient = useQueryClient();
  const session = useSession();
  const canOperator = roleAtLeast(session.data?.role, "operator");

  /* ------------------------------------------------------------ search */
  // The editor is SqlSearch's own state; loading a saved group's query
  // remounts it with that value (SqlSearch reads `value` only on mount).
  const [editorText, setEditorText] = useState(DEFAULT_SQL);
  const [editorSeq, setEditorSeq] = useState(0);
  const [runQuery, setRunQuery] = useState<string | null>(null);

  const schema = useQuery({ queryKey: ["agents-query-schema"], queryFn: getQuerySchema });
  const types = useQuery({ queryKey: ["agent-types"], queryFn: getAgentTypes, staleTime: 5 * 60_000 });
  const groups = useQuery({ queryKey: ["groups"], queryFn: listGroups });

  const search = useQuery({
    queryKey: ["agents", { q: runQuery }],
    queryFn: () => listAgents({ q: runQuery ?? undefined }),
    enabled: runQuery !== null,
  });

  function loadQuery(text: string) {
    setEditorText(text || "");
    setEditorSeq((n) => n + 1);
    setRunQuery(null);
  }

  const attributeColumns = (schema.data?.columns ?? []).filter((c) => c.kind === "attribute");

  /* ------------------------------------------------------------- saved */
  const [selectedName, setSelectedName] = useState<string>("");
  const [preview, setPreview] = useState<GroupPreview | null>(null);
  const [notice, setNotice] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [editQuery, setEditQuery] = useState<string | null>(null);
  const [editDescription, setEditDescription] = useState<string | null>(null);

  const byName = new Map((groups.data?.groups ?? []).map((g) => [g.name, g]));
  const selected: GroupView | undefined = selectedName ? byName.get(selectedName) : undefined;

  function selectGroup(name: string) {
    setSelectedName(name);
    setPreview(null);
    setNotice(null);
    setEditQuery(null);
    setEditDescription(null);
  }

  function invalidate() {
    void queryClient.invalidateQueries({ queryKey: ["groups"] });
  }

  const previewMutation = useMutation({
    mutationFn: (name: string) => previewGroup(byName.get(name)!.group_id),
    onSuccess: setPreview,
    onError: (error) => setNotice({ tone: "error", text: `Preview refused: ${errorText(error)}` }),
  });

  const deleteMutation = useMutation({
    mutationFn: (name: string) => deleteGroup(byName.get(name)!.group_id),
    onSuccess: (_result, name) => {
      setDeleteOpen(false);
      // Clear the selection first — selectGroup resets the notice slot.
      if (selectedName === name) selectGroup("");
      setNotice({ tone: "ok", text: `Deleted ${name} — the name is free for reuse.` });
      invalidate();
    },
    onError: (error) => setNotice({ tone: "error", text: `Delete refused: ${errorText(error)}` }),
  });

  const editMutation = useMutation({
    mutationFn: (name: string) => {
      const body: Record<string, unknown> = {};
      if ((editQuery ?? "").trim() && editQuery !== selected?.query) body.query = editQuery;
      if (editDescription !== null && editDescription !== (selected?.description ?? "")) {
        body.description = editDescription;
      }
      return updateGroup(byName.get(name)!.group_id, body);
    },
    onSuccess: (_result, name) => {
      setNotice({ tone: "ok", text: `Updated ${name}.` });
      setEditQuery(null);
      setEditDescription(null);
      invalidate();
    },
    onError: (error) => setNotice({ tone: "error", text: `Edit refused: ${errorText(error)}` }),
  });

  /* ------------------------------------------------------------ create */
  const [newName, setNewName] = useState("");
  const [newOwner, setNewOwner] = useState("");
  const [newInclude, setNewInclude] = useState("");
  const [newExclude, setNewExclude] = useState("");
  const [newDescription, setNewDescription] = useState("");

  const createMutation = useMutation({
    mutationFn: () =>
      createGroup({
        name: newName.trim(),
        query: editorText.trim() || DEFAULT_SQL,
        include: pinList(newInclude),
        exclude: pinList(newExclude),
        description: newDescription.trim() || null,
        owner: newOwner.trim() || null,
      }),
    onSuccess: (created) => {
      setNotice({ tone: "ok", text: `Saved ${created.name} with ${created.member_count} member(s). Saving a group pushes nothing.` });
      setNewName("");
      setNewInclude("");
      setNewExclude("");
      setNewDescription("");
      invalidate();
    },
    onError: (error) => setNotice({ tone: "error", text: `Group refused: ${errorText(error)}` }),
  });

  const inputClass =
    "w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1.5 text-sm";

  return (
    <section className="flex flex-col gap-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-lg font-semibold">Groups</h1>
        <span className="text-xs text-[var(--color-muted)]">
          {groups.data ? `${groups.data.count} saved` : ""}
        </span>
      </div>
      <p className="max-w-prose text-xs text-[var(--color-muted)]">
        A group is a <strong>saved SQL selection</strong>: search first, then name the query.
        Membership is resolved on read, so a group cannot drift from the fleet.
      </p>

      {notice && (
        <p
          role="status"
          className={
            "rounded-md border px-3 py-2 text-xs " +
            (notice.tone === "ok"
              ? "border-[var(--color-border)] text-[var(--color-text)]"
              : "border-[var(--color-danger)]/60 text-[var(--color-danger)]")
          }
        >
          {notice.text}
        </p>
      )}

      {/* 1. Search agents ------------------------------------------------ */}
      <Section
        title="1 · Search agents"
        caption="Only SELECT from agents. A saved group may not use LIMIT or OFFSET."
      >
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-end gap-2">
            <label className="flex min-w-56 flex-1 flex-col gap-1 text-xs">
              <span className="font-medium">Load a saved group's query</span>
              <select
                value=""
                onChange={(e) => {
                  const group = byName.get(e.target.value);
                  loadQuery(group?.query ?? "");
                }}
                className={inputClass}
              >
                <option value="">— choose a saved group —</option>
                {[...byName.keys()].sort().map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <SqlSearch
            key={editorSeq}
            columns={schema.data?.columns ?? []}
            value={editorText}
            label="SQL selection"
            onSubmit={(text) => setRunQuery(text)}
          />
          {runQuery !== null && (
            <div data-testid="search-result">
              <p className="text-xs text-[var(--color-muted)]">
                Agents matched: <strong>{search.data?.count ?? 0}</strong>
              </p>
              {search.isError && (
                <p className="text-xs text-[var(--color-danger)]">
                  Query refused: {errorText(search.error)}
                </p>
              )}
              {(search.data?.count ?? 0) === 0 && !search.isError && (
                <p className="mt-1 max-w-prose text-xs text-[var(--color-muted)]">
                  No agents matched. Check the attribute names in the suggestions — an unknown
                  attribute matches nothing rather than failing the query.
                </p>
              )}
              {(search.data?.agents ?? []).length > 0 && (
                <div className="mt-2 overflow-x-auto rounded-lg border border-[var(--color-border)]">
                  <table className="w-full min-w-[40rem] text-left text-sm">
                    <thead>
                      <tr className="border-b border-[var(--color-border)] text-xs uppercase tracking-wide text-[var(--color-muted)]">
                        <th scope="col" className="px-3 py-2 font-medium">Agent</th>
                        <th scope="col" className="px-3 py-2 font-medium">Service</th>
                        <th scope="col" className="px-3 py-2 font-medium">Health</th>
                        <th scope="col" className="px-3 py-2 font-medium">Config</th>
                        <th scope="col" className="px-3 py-2 font-medium">Type</th>
                        <th scope="col" className="px-3 py-2 font-medium">Environment</th>
                      </tr>
                    </thead>
                    <tbody>
                      {search.data!.agents.map((agent) => (
                        <tr key={agent.id} className="border-b border-[var(--color-border)] last:border-0">
                          <td className="px-3 py-2">
                            <Link
                              to="/agents/$agentId"
                              params={{ agentId: agent.id }}
                              className="font-medium hover:text-[var(--color-accent)] hover:underline"
                              title={agent.id}
                            >
                              {agent.name ?? <em className="text-[var(--color-muted)]">unnamed</em>}
                            </Link>
                          </td>
                          <td className="px-3 py-2 text-xs">{attrValue(agent.description, "service.name") ?? "—"}</td>
                          <td className="px-3 py-2 text-xs">
                            <StatusBadge
                              tone={agent.healthy === true ? "ok" : agent.healthy === false ? "danger" : "muted"}
                              label={agent.healthy === true ? "Healthy" : agent.healthy === false ? "Unhealthy" : "Unknown"}
                            />
                          </td>
                          <td className="px-3 py-2 text-xs"><StatusBadge status={agent.remote_config_status} /></td>
                          <td className="px-3 py-2 text-xs">
                            <AgentTypeBadge agentType={agent.agent_type} source={agent.agent_type_source} profiles={types.data?.types} />
                          </td>
                          <td className="px-3 py-2 text-xs">{attrValue(agent.description, "deployment.environment") ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
          {attributeColumns.length > 0 && (
            <details>
              <summary className="cursor-pointer text-xs text-[var(--color-muted)] hover:text-[var(--color-text)]">
                Columns and values observed on connected agents
              </summary>
              <div className="mt-2 overflow-x-auto rounded-md border border-[var(--color-border)]">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-[var(--color-border)] text-[var(--color-muted)]">
                      <th scope="col" className="px-2 py-1.5 font-medium">Column</th>
                      <th scope="col" className="px-2 py-1.5 font-medium">Agents</th>
                      <th scope="col" className="px-2 py-1.5 font-medium">Values</th>
                    </tr>
                  </thead>
                  <tbody>
                    {attributeColumns.map((c) => (
                      <tr key={c.name} className="border-b border-[var(--color-border)] last:border-0">
                        <td className="px-2 py-1.5 font-mono">{c.sql_name}</td>
                        <td className="px-2 py-1.5">{c.agent_count}</td>
                        <td className="px-2 py-1.5 font-mono text-[var(--color-muted)]">
                          {(c.values ?? []).join(", ").slice(0, 120)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          )}
        </div>
      </Section>

      {/* 2. Save as a group ---------------------------------------------- */}
      <Section
        title="2 · Save this query as a group"
        caption="Saving a group pushes nothing. A group with zero members is valid to save and impossible to push to."
        actions={
          !canOperator && (
            <span className="text-[11px] text-[var(--color-muted)]">needs the operator role</span>
          )
        }
      >
        <form
          className="grid gap-3 md:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault();
            createMutation.mutate();
          }}
        >
          <label className="flex flex-col gap-1 text-xs">
            <span className="font-medium">Name</span>
            <input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="macos-au"
              required
              className={inputClass}
            />
            <span className="text-[10px] text-[var(--color-muted)]">
              Lowercase a-z, 0-9, '.', '_' and '-'; unique regardless of case.
            </span>
          </label>
          <label className="flex flex-col gap-1 text-xs">
            <span className="font-medium">Owner</span>
            <input value={newOwner} onChange={(e) => setNewOwner(e.target.value)} placeholder="platform-oncall" className={inputClass} />
          </label>
          <label className="flex flex-col gap-1 text-xs">
            <span className="font-medium">Force in (agent ids, comma separated)</span>
            <input value={newInclude} onChange={(e) => setNewInclude(e.target.value)} className={inputClass} />
          </label>
          <label className="flex flex-col gap-1 text-xs">
            <span className="font-medium">Force out (agent ids, comma separated)</span>
            <input value={newExclude} onChange={(e) => setNewExclude(e.target.value)} className={inputClass} />
          </label>
          <label className="flex flex-col gap-1 text-xs md:col-span-2">
            <span className="font-medium">Description</span>
            <input
              value={newDescription}
              onChange={(e) => setNewDescription(e.target.value)}
              placeholder="macOS gateways in Australia. Owner: platform-oncall."
              className={inputClass}
            />
          </label>
          <div className="md:col-span-2">
            <p className="mb-2 text-[11px] text-[var(--color-muted)]">
              Query saved: <code className="font-mono">{editorText.trim() || DEFAULT_SQL}</code> — pins are human overrides
              that still apply when the query does not match.
            </p>
            <button
              type="submit"
              disabled={!canOperator || createMutation.isPending}
              className="rounded-md border border-[var(--color-accent)] bg-[var(--color-accent)]/10 px-3 py-1.5 text-sm disabled:opacity-40"
            >
              Save group
            </button>
          </div>
        </form>
      </Section>

      {/* 3. Saved groups --------------------------------------------------- */}
      <Section title="3 · Saved groups">
        {(groups.data?.groups ?? []).length === 0 ? (
          <EmptyState
            title="No groups saved yet"
            detail="Run a query above and save it to create one."
          />
        ) : (
          <div className="flex flex-col gap-3">
            <div className="overflow-x-auto rounded-lg border border-[var(--color-border)]">
              <table className="w-full min-w-[40rem] text-left text-sm">
                <thead>
                  <tr className="border-b border-[var(--color-border)] text-xs uppercase tracking-wide text-[var(--color-muted)]">
                    <th scope="col" className="px-3 py-2 font-medium">Group</th>
                    <th scope="col" className="px-3 py-2 font-medium">Kind</th>
                    <th scope="col" className="px-3 py-2 font-medium">Members</th>
                    <th scope="col" className="px-3 py-2 font-medium">Health-defined</th>
                    <th scope="col" className="px-3 py-2 font-medium">Owner</th>
                    <th scope="col" className="px-3 py-2 font-medium">Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {groups.data!.groups.map((g) => (
                    <tr
                      key={g.group_id}
                      onClick={() => selectGroup(g.name)}
                      className={
                        "cursor-pointer border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-bg)] " +
                        (g.name === selectedName ? "bg-[var(--color-bg)]" : "")
                      }
                    >
                      <td className="px-3 py-2 font-medium">{g.name}</td>
                      <td className="px-3 py-2 text-xs">{g.selector_kind}</td>
                      <td className="px-3 py-2 text-xs">{g.member_count}</td>
                      <td className="px-3 py-2 text-xs">{g.health_defined.join(", ") || "—"}</td>
                      <td className="px-3 py-2 text-xs">{g.owner ?? "—"}</td>
                      <td className="px-3 py-2 text-xs">{formatWhen(g.updated_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {selected && (
              <div data-testid="group-detail" className="rounded-md border border-[var(--color-border)] p-3">
                <div className="mb-2 flex items-baseline justify-between gap-3">
                  <h3 className="text-sm font-semibold">{selected.name}</h3>
                  <span className="text-xs text-[var(--color-muted)]">
                    {selected.member_count} member(s) now
                    {selected.description ? ` · ${selected.description}` : ""}
                  </span>
                </div>
                {selected.selector_kind === "query" ? (
                  <pre className="overflow-x-auto rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] p-2 font-mono text-xs">
                    {selected.query}
                  </pre>
                ) : (
                  <p className="text-xs text-[var(--color-muted)]">
                    Legacy filter-document group (no SQL query yet) — replace its definition with a query below.
                  </p>
                )}
                <p className="mt-2 text-[11px] text-[var(--color-muted)]">
                  {selected.sources?.query?.length ?? selected.sources?.definition?.length ?? 0} from the selector ·{" "}
                  {selected.sources?.included?.length ?? 0} pinned in · {selected.sources?.excluded?.length ?? 0} pinned out
                  {selected.membership_changes > 0 && ` · resolution changed ${selected.membership_changes}×`}
                </p>
                {(selected.unknown_pins ?? []).length > 0 && (
                  <p className="mt-1 text-[11px] text-[var(--color-warn)]">
                    Pins for agents that have never reported: {selected.unknown_pins!.join(", ")}
                  </p>
                )}
                {selected.health_defined.length > 0 && (
                  <p className="mt-1 text-[11px] text-[var(--color-warn)]">
                    Filters on {selected.health_defined.join(", ")}: pushing to it is refused unless the stored
                    membership is used (agents drop out exactly when they break).
                  </p>
                )}

                {preview && (
                  <div data-testid="group-preview" className="mt-2 rounded-md border border-[var(--color-border)] p-2 text-xs">
                    <p>
                      Members now: <strong>{preview.count}</strong> · previously {preview.previous_count} ·{" "}
                      {preview.dropped.length} dropped · {preview.added.length} added
                    </p>
                    {preview.dropped.length > 0 && <p>Dropped: {preview.dropped.join(", ")}</p>}
                    {preview.added.length > 0 && <p>Added: {preview.added.join(", ")}</p>}
                  </div>
                )}

                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => previewMutation.mutate(selected.name)}
                    disabled={previewMutation.isPending}
                    className="rounded-md border border-[var(--color-border)] px-2 py-1 text-xs hover:text-[var(--color-text)]"
                  >
                    Preview membership
                  </button>
                  <button
                    type="button"
                    onClick={() => setDeleteOpen(true)}
                    disabled={!canOperator}
                    className="rounded-md border border-[var(--color-border)] px-2 py-1 text-xs text-[var(--color-danger)] disabled:opacity-40"
                  >
                    Delete group
                  </button>
                </div>

                <form
                  className="mt-3 grid gap-2"
                  onSubmit={(e) => {
                    e.preventDefault();
                    editMutation.mutate(selected.name);
                  }}
                >
                  <label className="flex flex-col gap-1 text-xs">
                    <span className="font-medium">
                      Edit the query — future pushes only; a revision keeps its snapshot
                    </span>
                    <textarea
                      value={editQuery ?? selected.query ?? ""}
                      onChange={(e) => setEditQuery(e.target.value)}
                      rows={3}
                      spellCheck={false}
                      className="rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] p-2 font-mono text-xs"
                      placeholder={selected.query ? "" : "SELECT * FROM agents WHERE ..."}
                    />
                  </label>
                  <label className="flex flex-col gap-1 text-xs">
                    <span className="font-medium">Description</span>
                    <input
                      value={editDescription ?? selected.description ?? ""}
                      onChange={(e) => setEditDescription(e.target.value)}
                      className={inputClass}
                    />
                  </label>
                  <div>
                    <button
                      type="submit"
                      disabled={!canOperator || editMutation.isPending}
                      className="rounded-md border border-[var(--color-border)] px-2 py-1 text-xs disabled:opacity-40"
                    >
                      Save query
                    </button>
                  </div>
                </form>
              </div>
            )}
          </div>
        )}
      </Section>

      <Modal open={deleteOpen} title={`Delete ${selectedName}?`} onClose={() => setDeleteOpen(false)}>
        <p className="text-xs text-[var(--color-muted)]">
          The group is gone and the name is free for reuse. History needs no live row: every
          revision carries its own group snapshot. A live rollout using this group refuses the
          delete with 409.
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={() => setDeleteOpen(false)}
            className="rounded-md border border-[var(--color-border)] px-3 py-1.5 text-xs"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => deleteMutation.mutate(selectedName)}
            disabled={deleteMutation.isPending}
            className="rounded-md border border-[var(--color-danger)]/60 bg-[var(--color-danger)]/10 px-3 py-1.5 text-xs text-[var(--color-danger)]"
          >
            Delete group
          </button>
        </div>
      </Modal>
    </section>
  );
}
