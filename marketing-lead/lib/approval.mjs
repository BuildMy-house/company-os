// Approval gate shared by every publish path. Hermes is the approver; the marketing-lead only drafts
// unless a publish ticket carries an approval_ref. Presence and format are checked here;
// authenticity cannot be (Hive does not expose payload metadata to the pod, docs/MARKETING-LEAD.md).
import { appendFileSync, readFileSync } from "node:fs";

// Same pattern as scripts/engineering-manager-mcp.js and company_ops/marketing_sidecar.py.
export const APPROVAL_REF_RE = /^[A-Za-z0-9][A-Za-z0-9._:/#-]{5,119}$/;

export class Refused extends Error {}

export function requireApproval(ref, what) {
  if (typeof ref !== "string" || !APPROVAL_REF_RE.test(ref)) {
    throw new Refused(`${what} refused: a valid approval_ref from Hermes is required to publish. Without it the marketing-lead only drafts. Ask Hermes to resubmit the ticket with metadata.approval_ref.`);
  }
  return ref;
}

export class Ledger {
  constructor(path) { this.path = path; }
  entries() {
    try { return readFileSync(this.path, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)); } catch (e) { if (e.code === "ENOENT") return []; throw e; }
  }
  append(entry) { appendFileSync(this.path, JSON.stringify(entry) + "\n"); }
  // One publish per approval_ref, and at most `cap` per UTC day per channel.
  check(channel, ref, cap, now = new Date()) {
    const all = this.entries();
    if (all.some((e) => e.approval_ref === ref && e.channel === channel)) throw new Refused(`${channel} publish refused: approval_ref ${ref} was already used.`);
    const day = now.toISOString().slice(0, 10);
    if (all.filter((e) => e.channel === channel && String(e.ts).slice(0, 10) === day).length >= cap) throw new Refused(`${channel} publish refused: daily cap (${cap}) reached.`);
  }
}
