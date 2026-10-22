"""
Approval policy registration.

The active policy (config/approval_policy.yaml) is stored with its file
hash, and each change of active policy is appended to
approval_policy_activations. Views read the latest activation, so flipping
require_independent_approval re-evaluates every override immediately,
with no edits to recorded decisions or approvals.

If no policy is active, the views assume the STRICT rule (independent
approval required): a missing config can never loosen verification.
"""
from __future__ import annotations

import sqlite3

from src.config import ApprovalPolicy
from src.storage.db import utc_now


class PolicyVersionError(RuntimeError):
    pass


def register_policy(conn: sqlite3.Connection, policy: ApprovalPolicy) -> None:
    row = conn.execute("SELECT config_hash, require_independent_approval FROM approval_policies "
                       "WHERE version = ?", (policy.version,)).fetchone()
    with conn:
        if row is None:
            conn.execute("""INSERT INTO approval_policies
                            (version, require_independent_approval, config_hash, registered_at)
                            VALUES (?, ?, ?, ?)""",
                         (policy.version, int(policy.require_independent_approval),
                          policy.config_hash, utc_now()))
        elif row[0] != policy.config_hash:
            raise PolicyVersionError(
                f"config/approval_policy.yaml changed but version {policy.version!r} was not "
                "bumped. Give the changed file a new version.")
        active = conn.execute("SELECT version FROM approval_policy_activations "
                              "ORDER BY activation_id DESC LIMIT 1").fetchone()
        if active is None or active[0] != policy.version:
            conn.execute("INSERT INTO approval_policy_activations (version, activated_at) "
                         "VALUES (?, ?)", (policy.version, utc_now()))
