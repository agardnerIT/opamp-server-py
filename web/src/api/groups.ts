/**
 * Saved groups (new-ui stage 7): the full CRUD over `POST/GET/PUT/DELETE
 * /groups`, plus the membership preview. The read-only `listGroups()` used by
 * the config stepper stays in `config.ts` — that page only reads.
 *
 * A group is a **saved SQL selection** (or a legacy filter document): search
 * first, name it second. Membership is resolved on read server-side, so the
 * UI never caches a member list as truth — every count on the page comes from
 * the last fetch.
 */

import { api } from "./client";
import { unwrap } from "./errors";

/**
 * `groups.view()` (server/groups.py) — the list entries carry the same shape;
 * the single-group GET additionally carries `resolutions`.
 */
export interface GroupView {
  group_id: string;
  name: string;
  /** `query` (SQL) or `definition` (legacy filter document). */
  selector_kind: string;
  query: string | null;
  definition: unknown;
  include: string[];
  exclude: string[];
  description: string | null;
  owner: string | null;
  source: string | null;
  created_by: string | null;
  created_at: string | null;
  updated_by: string | null;
  updated_at: string | null;
  member_count: number;
  members: string[];
  /** Which members came from the selector vs the human pins. */
  sources: Record<string, string[]> | null;
  /** Pins for agents that have never reported — a typo'd id otherwise looks like a working pin forever. */
  unknown_pins: string[] | null;
  /** Health keys the selector uses — pushing to the group is guarded server-side. */
  health_defined: string[];
  membership_changes: number;
  resolutions?: unknown[];
}

export interface GroupsResponse {
  groups: GroupView[];
  count: number;
}

/** `POST /groups` body — exactly one selector (`query` XOR `definition`). */
export interface GroupCreateInput {
  name: string;
  query?: string | null;
  definition?: Record<string, unknown> | null;
  include?: string[];
  exclude?: string[];
  description?: string | null;
  owner?: string | null;
}

/** `PUT /groups/{id}` body — only present fields change. */
export interface GroupUpdateInput {
  name?: string | null;
  query?: string | null;
  definition?: Record<string, unknown> | null;
  include?: string[] | null;
  exclude?: string[] | null;
  description?: string | null;
  owner?: string | null;
}

/** `POST /groups/{id}/preview` — members now vs the last recorded resolution. */
export interface GroupPreview {
  group_id: string;
  name: string;
  count: number;
  previous_count: number;
  added: string[];
  dropped: string[];
  unchanged: string[];
  sources: Record<string, string[]> | null;
  unknown_pins: string[] | null;
}

export async function listGroups(): Promise<GroupsResponse> {
  return unwrap<GroupsResponse>(api.GET("/groups"));
}

export async function getGroup(groupId: string): Promise<GroupView> {
  return unwrap<GroupView>(
    api.GET("/groups/{group_id}", { params: { path: { group_id: groupId } } }),
  );
}

export async function createGroup(input: GroupCreateInput): Promise<GroupView> {
  return unwrap<GroupView>(api.POST("/groups", { body: input }));
}

export async function updateGroup(
  groupId: string,
  input: GroupUpdateInput,
): Promise<GroupView> {
  return unwrap<GroupView>(
    api.PUT("/groups/{group_id}", {
      params: { path: { group_id: groupId } },
      body: input,
    }),
  );
}

/** 409 while a live rollout uses the group (the name is free for reuse after). */
export async function deleteGroup(groupId: string): Promise<{ deleted: string; name: string }> {
  return unwrap<{ deleted: string; name: string }>(
    api.DELETE("/groups/{group_id}", { params: { path: { group_id: groupId } } }),
  );
}

export async function previewGroup(groupId: string): Promise<GroupPreview> {
  return unwrap<GroupPreview>(
    api.POST("/groups/{group_id}/preview", { params: { path: { group_id: groupId } } }),
  );
}
