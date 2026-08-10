"""Generate the v1 should_decompose dataset (train/eval/canaries).

Method: rule-based enumeration over the evidence rules in the owning module.
Each candidate (task, context) pair is hand-authored prose mapped to an
intended evidence profile; the gold label, reason, and confidence are computed
by running the module's neutral JSON CLI, so every record is self-consistent
with the current rule engine by construction. The script asserts the engine's
decision matches the intended label for every record before writing anything.

Records follow the dataset-format.md envelope. Module fields
``input``/``expected`` stay at top level (the authoritative schema of the
current ``ExampleDatasetLoader``); ``expected_confidence`` and
``rationale_keywords`` carry the spec's ``ShouldDecomposeDatum``
semantics. Canary records add ``canary: true`` and ``canary_info``.

Run: python3.12 scripts/generate_should_decompose_v1.py
"""

# The hand-authored dataset sentences intentionally remain readable as single
# strings; existing E501 findings in this data are not executable logic.
# ruff: noqa: E501

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cambium.modules.base import (  # noqa: E402
    ModuleBoundaryError,
    load_module_manifest,
    run_module_cli,
)

ADDED_AT = "2026-08-09"
ADDED_BY = "agent:data-builder-v1"
DATASET_VERSION = "1.1.0"
SCHEMA_VERSION = 1
DEFAULT_LOGICAL_MODULE = "should_decompose"


def _resolve_manifest(logical_name: str = DEFAULT_LOGICAL_MODULE):
    """Resolve the owning module through its module.json, never a package path."""
    modules_dir = ROOT / "src" / "cambium" / "modules"
    if not modules_dir.is_dir():
        return None
    for child in sorted(modules_dir.iterdir()):
        if not child.is_dir() or not (child / "module.json").is_file():
            continue
        try:
            manifest = load_module_manifest(child, child.name)
        except ModuleBoundaryError:
            continue
        if manifest.module_name == logical_name:
            return manifest
    return None


MANIFEST = _resolve_manifest()

T = True
F = False


def rec(task: str, label: bool, context: str = "", note: str = "") -> dict:
    return {"task": task, "context": context, "label": label, "note": note}


# ---------------------------------------------------------------------------
# TRAIN
# ---------------------------------------------------------------------------

# --- FALSE: evidence 0 (atomic) -------------------------------------------------
F_ATOMIC = [
    rec("Delete the unused caching layer from the payment service.", F),
    rec("Rename the `parser` module to `lexer`.", F),
    rec("Bump the dependency pin for requests to 2.31.0.", F),
    rec("Add a docstring to the retry decorator.", F),
    rec("Print the build timestamp to the deploy log.", F),
    rec("Fix the off-by-one error in the pagination loop.", F),
    rec("Update the copyright header in the license file.", F),
    rec("Install the operator bundle on the staging cluster.", F),
    rec("Rotate the expired signing key for the artifact registry.", F),
    rec("Refresh the TLS certificate on the ingress gateway.", F),
    rec("Set the log level for the audit service to debug.", F),
    rec("Pin the Terraform provider version in the root module.", F),
    rec("Migrate the dev database to the latest schema revision.", F),
    rec("Escalate the flaky test ticket to the platform team.", F),
    rec("Restart the stuck Airflow scheduler after the memory spike.", F),
    rec("Purge the stale sessions from the token cache.", F),
    rec("Correct the timezone offset in the invoice generator.", F),
    rec("Enable the feature flag for the new checkout flow in staging.", F),
    rec("Tune the garbage collector settings for the ingestion service.", F),
    rec("Remove the dead code path in the webhook parser.", F),
    rec("Archive the 2023 quarterly reports to cold storage.", F),
    rec("Add the missing index hint to the slow report query.", F),
    rec("Backfill the empty `status` column in the jobs table.", F),
    rec("Point the staging DNS record at the new load balancer.", F),
]

# --- FALSE: keyword decoy (2+ HIGH_SIGNAL, evidence 1) -------------------------
F_KEYWORDS = [
    rec("Coordinate the deploy with several other services and both the web and the worker pods.", F, note="decoy: 3 keywords, evidence 1"),
    rec("Keep the multiple feature branches in sync with both the shared config and the several environment files.", F, note="decoy: 3 keywords, evidence 1"),
    rec("Merge the components needed for the new dashboard without touching several shared utilities.", F, note="decoy: 2 keywords, evidence 1"),
    rec("Present the rollout plan covering both the API and the worker to the multiple review groups.", F, note="decoy: 2 keywords, evidence 1"),
    rec("Align the several configuration files and both deployment templates before the freeze.", F, note="decoy: 2 keywords, evidence 1"),
    rec("Prepare the environment for the several changes across both the backend and the multiple frontend apps.", F, note="decoy: 3 keywords, evidence 1"),
    rec("Confirm the services agree on the schema before the multiple teams merge their work.", F, note="decoy: 2 keywords, evidence 1"),
    rec("Document the several provider quirks for both the sync and the async adapters.", F, note="decoy: 2 keywords, evidence 1"),
    rec("Review the components of the proposal with several stakeholders before sign-off.", F, note="decoy: 2 keywords, evidence 1"),
    rec("Check both the audit trail and the multiple dashboards for the same metric.", F, note="decoy: 2 keywords, evidence 1"),
    rec("Schedule the several data refreshes and both replication jobs for the weekend.", F, note="decoy: 2 keywords, evidence 1"),
    rec("Compare the outputs of the multiple pipelines across the several input shapes.", F, note="decoy: 2 keywords, evidence 1"),
    rec("Track the several dependencies and both their licenses in the compliance report.", F, note="decoy: 2 keywords, evidence 1"),
    rec("Summarize the incident for both the execs and the multiple engineering pods.", F, note="decoy: 2 keywords, evidence 1"),
]

# --- FALSE: sentence decoy (3+ sentences, evidence 1) --------------------------
F_SENTENCES = [
    rec("The nightly report needs a small formatting tweak. The header row is misaligned by one column. The legend labels are truncated at the right edge.", F, note="decoy: 3 sentences, evidence 1"),
    rec("The CLI hangs on large inputs. The progress bar renders after the work is done. The error message does not wrap correctly.", F, note="decoy: 3 sentences, evidence 1"),
    rec("The service descriptor is out of date. It still lists the retired endpoint. The health check ignores the new port.", F, note="decoy: 3 sentences, evidence 1"),
    rec("The auth cookie expires too early. Users get logged out mid-session. The refresh handler does not renew it.", F, note="decoy: 3 sentences, evidence 1"),
    rec("The migration script fails on the second batch. The offset column is not sorted. The checkpoint file is never written.", F, note="decoy: 3 sentences, evidence 1"),
    rec("The cache miss rate jumped after the last deploy. The TTL setting was reset to its default. The eviction policy changed without notice.", F, note="decoy: 3 sentences, evidence 1"),
    rec("The dashboard widget shows stale numbers. The refresh interval is hard-coded. The backend endpoint ignores the query param.", F, note="decoy: 3 sentences, evidence 1"),
    rec("The queue depth climbs under load. The consumer batch size stays fixed. The prefetch count is set too low.", F, note="decoy: 3 sentences, evidence 1"),
    rec("The SDK wrapper leaks file descriptors. The context is never cancelled. The connection pool is not drained.", F, note="decoy: 3 sentences, evidence 1"),
    rec("The index rebuild blocks new writes. The old index is still active. The migration order needs a rethink.", F, note="decoy: 3 sentences, evidence 1"),
    rec("The retry budget is exhausted by lunch. The backoff ceiling is too generous. The circuit opens on every 5xx response.", F, note="decoy: 3 sentences, evidence 1"),
    rec("The release notes skip a breaking change. The changelog generator misses merge commits. The version bump is not enforced.", F, note="decoy: 3 sentences, evidence 1"),
]

# --- FALSE: length decoy (>220 chars, evidence 1) ------------------------------
F_LENGTH = [
    rec("Explain the drift between the configuration manifest and the running cluster state by walking through the operator's reconcile loop, the webhook validation chain, the schema conversion rules, the status subresource updates, and the finalizer ordering before any upgrade is attempted on the affected namespaces.", F, note="decoy: >220 chars, evidence 1"),
    rec("Reproduce the intermittent connection resets by replaying the recorded traffic through the proxy with the original timing, the same TLS fingerprint, the identical client headers, and the unchanged backoff schedule, then compare the stack traces against the baseline captured before the last kernel update.", F, note="decoy: >220 chars, evidence 1"),
    rec("Document the expected behavior of the transfer service under partial failures, covering the idempotency semantics, the ordering guarantees, the delivery window, the poison-message handling, and the reconciliation steps that run after the outage window has expired and before the daily summary is published to the ops channel.", F, note="decoy: >220 chars, evidence 1"),
    rec("Measure the impact of the new compression scheme on cold-start latency by sampling the first five requests across all the regional zones, the warm cache states, the payload distributions, and the client protocol versions, then compare the p99 numbers with the previous quarter's baseline.", F, note="decoy: >220 chars, evidence 1"),
    rec("Trace the root cause of the elevated error rate from the new release by correlating the gateway logs, the application traces, the database metrics, and the client-side reports across the deployment window, the canary rollout phases, and the rollback timeline from the previous Friday's incident.", F, note="decoy: >220 chars, evidence 1"),
    rec("Verify the claim in the ticket about the flaky integration suite by running the affected specs against the merged branch, the release candidate, and the nightly build, then compare the failure sets with the recorded history from the last two weeks.", F, note="decoy: >220 chars, evidence 1"),
]

# --- FALSE: "each" decoy (evidence 1) ------------------------------------------
F_EACH = [
    rec("Normalize each timestamp to UTC before the comparison.", F, note="decoy: 'each', evidence 1"),
    rec("Trim each line before writing it to the output buffer.", F, note="decoy: 'each', evidence 1"),
    rec("Encrypt each field with its own derived key.", F, note="decoy: 'each', evidence 1"),
    rec("Validate each request against the same schema.", F, note="decoy: 'each', evidence 1"),
    rec("Hash each file in place before the transfer.", F, note="decoy: 'each', evidence 1"),
    rec("Compile each module with the same feature set.", F, note="decoy: 'each', evidence 1"),
]

# --- FALSE: file-ref decoy (3+ file refs, evidence 1) --------------------------
F_FILES = [
    rec("Refresh the type stubs in api.py, models.py, and client.py against the new schema.", F, note="decoy: 3 file refs, evidence 1"),
    rec("Fix the formatting drift in config.yaml, settings.toml, and the generated schema.json.", F, note="decoy: 3 file refs, evidence 1"),
    rec("Sync the version pins across pyproject.toml, Cargo.toml, and package.json.", F, note="decoy: 3 file refs, evidence 1"),
    rec("Normalize the whitespace in README.md, CONTRIBUTING.md, docs/HACKING.md, and the style guide.", F, note="decoy: 3 file refs, evidence 1"),
    rec("Correct the quoting in db/seed.sql, db/migrate.sql, and queries.sql.", F, note="decoy: 3 file refs, evidence 1"),
    rec("Regenerate the mocks in test_http.ts, test_ws.ts, and test_auth.ts after the interface change.", F, note="decoy: 3 file refs, evidence 1"),
    rec("Align the copyright notices in go.mod, go.sum, and NOTICE.md.", F, note="decoy: 3 file refs, evidence 1"),
    rec("Prune the stale entries in the run scripts for the deploy job: deploy.sh, rollback.sh, and health.sh.", F, note="decoy: 3 file refs, evidence 1"),
]

# --- FALSE: exactly-2-verb decoy (evidence 1) ----------------------------------
F_2VERBS = [
    rec("Add the retry decorator, update the error handler, and run the integration suite for the gateway client.", F, note="decoy: 2 verb-led clauses, evidence 1"),
    rec("Create the config schema, refactor the loader, and document the new defaults.", F, note="decoy: 2 verb-led clauses, evidence 1"),
    rec("Build the release image, create the release branch, and draft the release notes.", F, note="decoy: 2 verb-led clauses, evidence 1"),
    rec("Fix the memory leak, remove the dead code, and rerun the soak test for one hour.", F, note="decoy: 2 verb-led clauses, evidence 1"),
    rec("Implement the search endpoint, update the OpenAPI spec, and wire up the tests.", F, note="decoy: 2 verb-led clauses, evidence 1"),
    rec("Migrate the legacy queue, add the consumer group config, and watch for lag.", F, note="decoy: 2 verb-led clauses, evidence 1"),
    rec("Introduce the tenant id filter, restructure the query builder, and keep the API compatible.", F, note="decoy: 2 verb-led clauses, evidence 1"),
    rec("Port the metrics exporter, update the dashboards, and smoke-test the alert path.", F, note="decoy: 2 verb-led clauses, evidence 1"),
    rec("Rewrite the rate limiter, backfill the throttle table, and compare the benchmark numbers.", F, note="decoy: 2 verb-led clauses, evidence 1"),
    rec("Split the monolith module, create the shared contract, and schedule the cutover.", F, note="decoy: 2 verb-led clauses, evidence 1"),
    rec("Add the audit fields, migrate the write path, and verify the read path stays identical.", F, note="decoy: 2 verb-led clauses, evidence 1"),
    rec("Refactor the auth middleware, update the session store, and confirm the existing tokens still work.", F, note="decoy: 2 verb-led clauses, evidence 1"),
]

# --- FALSE: context suppression -------------------------------------------------
F_CONTEXT = [
    rec("Add retries to the export job, add a resume point, and add a completion notification.", F, context="Subtasks are already planned for this epic."),
    rec("Update the checkout flow, update the payment hooks, and update the refund screen.", F, context="The decomposition of this work is already approved."),
    rec("Implement multi-provider support, add per-region failover, and update the config schema.", F, context="Prior subtask planning is in the tracker."),
    rec("Build the dashboard, build the report exporter, and build the alert router.", F, context="A subtask breakdown exists in the sprint board."),
    rec("Migrate the order service, migrate the inventory service, and migrate the search index.", F, context="The decomposition was reviewed last sprint."),
    rec("Fix the auth bug, fix the billing bug, and fix the notification bug.", F, context="Subtasks are tracked under the release epic."),
    rec("Add the new endpoints, add the schema changes, and add the client bindings.", F, context="Decomposition of the delivery is already done."),
    rec("Update the TLS settings, update the firewall rules, and update the audit logging.", F, context="The subtask plan is attached to the ticket."),
    rec("Create the staging environment, create the test fixtures, and create the load profile.", F, context="Decomposed work items already exist."),
    rec("Refactor the repository layer, refactor the service layer, and refactor the web layer.", F, context="A subtask list is available in the design doc."),
    rec("Rewrite the CLI parser, rewrite the output formatter, and rewrite the config loader.", F, context="The decomposition is pinned in the proposal."),
    rec("Backfill the user profiles, backfill the order history, and backfill the audit events.", F, context="Subtask breakdowns were filed earlier."),
]

# --- FALSE: hard mixed (competing near-miss signals, evidence 1) ---------------
F_HARD = [
    rec("The change touches several services but is one small fix, and it needs both the config and the runbook updated.", F, note="hard: 3 keywords, evidence 1"),
    rec("Split the module into two files, add the new exports, and keep the public API identical, but this is still a single atomic refactor.", F, note="hard: 2 verb-led clauses, evidence 1"),
    rec("The fix spans multiple layers and several services, but the change is a one-line guard in the router.", F, note="hard: 3 keywords, evidence 1"),
    rec("Port the library, update the two call sites, and rerun the full test suite, yet the work fits in a single pull request.", F, note="hard: 2 verb-led clauses, evidence 1"),
    rec("Introduce the new error format, migrate the logging calls, and keep the wire protocol untouched, while the rollout happens as one change.", F, note="hard: 2 verb-led clauses, evidence 1"),
    rec("The request touches both the cache and the queue, and several teams read the result, but the underlying change is a single configuration flag.", F, note="hard: 2 keywords, evidence 1"),
]

# --- TRUE: 3+ sentences + 2+ keywords ------------------------------------------
T_SENT_KW = [
    rec("Add per-tenant rate limiting to the gateway. Configure separate quota pools for every tier. Roll out the change independently across the several regions.", T),
    rec("Update the admin panel to the new design system. The settings pages move to the shared components. The web and the API changes ship in parallel.", T),
    rec("Implement the audit log retention policy. The exporter runs separately from the ingestion path. Both the raw and the aggregated streams are covered.", T),
    rec("Introduce the plugin registry for the CLI. The extensions register their hooks in the new manifest. The core stays independently testable while several tools share it.", T),
    rec("Add the dark mode toggle to the settings screen. The theme preference syncs across multiple devices. Support both the web and the mobile clients.", T),
    rec("Backfill the missing metrics for the past quarter. The dashboards update in parallel with the data pipeline. Several teams depend on the new breakdown.", T),
    rec("Refactor the scheduler to accept pluggable policies. Each worker fetches its assignments independently. The retry and the dead-letter paths stay separately managed.", T),
    rec("Migrate the on-prem cache to the managed service. The data moves in parallel to keep the window small. Both the read and the write paths need checks.", T),
    rec("Update the CI matrix to cover the new runtime. Several jobs currently build in parallel. The artifacts for multiple platforms are published.", T),
    rec("Create the staging mirrors for the package registry. The snapshots refresh separately from the release channel. Both mirrors share the same signing key.", T),
    rec("Rewrite the search indexer for the new tokenizer. The indexing and the query paths run in parallel. Several shard layouts are benchmarked.", T),
    rec("Add the observability exports for the new runtime. The metrics flow independently from the traces. Both streams land in the same sink.", T),
]

# --- TRUE: 3+ sentences + length ----------------------------------------------
T_SENT_LEN = [
    rec("The platform review flagged the following gaps in the incident workflow. The on-call handover does not preserve the escalation context. The paging policy skips the secondary responder when the primary is already engaged. The post-incident report omits the affected customer segments. The runbook links are stale and point to the old wiki.", T),
    rec("The schema change touches the core write path, the materialized views, the replication lag monitor, and the backup verification job, so the rollout plan needs review across the data platform team. The migration must also handle the partitioned tables from the previous release. The rollback procedure has to be tested before the change ships.", T),
    rec("We need to upgrade the message broker, which involves auditing the consumer offsets, the retention policies, the topic partitioning, and the client library versions across every service that publishes events. The upgrade also requires a compatibility pass over the stream processing jobs. The staging environment must mirror production traffic for a full week.", T),
    rec("The vulnerability report lists issues across the authentication flow, the session management, the password reset endpoint, the audit logging path, and the token refresh handler, and the remediation must be scheduled carefully to avoid breaking the existing clients. The dependency advisories also need to be triaged before the patch window opens. The affected services must be patched in the correct order during the maintenance window.", T),
    rec("The monorepo migration touches the build tooling, the CI pipeline definitions, the release automation, the dependency management layer, and the developer onboarding docs, and each piece of the old layout needs to be mapped to its new location before the cutover. The feature branches need to be reconciled with the new module boundaries. The tag strategy and the version scheme also require a decision before the final switch.", T),
    rec("The capacity planning review covers the traffic growth assumptions, the queue depth projections, the storage retention estimates, the network bandwidth requirements, and the cost model for the new regions, and the resulting plan needs to be reviewed with the platform owners before it is committed. The projection methodology should also account for the seasonal peaks observed in the last two years. The final document must be shared with the engineering leads ahead of the budget cycle.", T),
]

# --- TRUE: length + 2+ keywords ------------------------------------------------
T_LEN_KW = [
    rec("The migration plan for the storage layer must cover the hot path, the cold path, the compression profiles, the replication settings, and the backup schedules across several regions, and both the primary and the secondary clusters need a staged rollout so that the change can be validated before the remaining traffic is switched over during the maintenance window.", T),
    rec("The new onboarding flow needs to handle the invite emails, the identity verification steps, the team provisioning calls, the role assignment rules, and the audit trail entries across multiple workspaces, and the fallback behavior for partially completed registrations has to be tested separately from the happy path so that errors surface in the right queue.", T),
    rec("The observability overhaul covers the trace sampling policy, the log retention tiers, the metric cardinality controls, the alert routing rules, and the dashboard permissions across several environments, and the rollout of the new agent needs to happen in parallel with the collector upgrade so that the ingestion pipeline stays consistent throughout.", T),
    rec("The security review of the public endpoints found issues in the rate limiting logic, the request validation layer, the response headers, the authentication bypass path, and the error handling flow, and the fixes need to be applied across multiple regions and several deployment zones without breaking the existing clients that depend on the current behavior.", T),
    rec("The release checklist for the quarterly rollout covers the feature flags, the database migrations, the cache invalidation steps, the documentation updates, and the rollback plan, and both the staging and the production approvals must be recorded separately so that the audit trail stays complete for compliance review.", T),
    rec("The upgrade path for the protocol library must include the handshake changes, the message framing updates, the connection retry policy, the timeout adjustments, and the client compatibility layer across several supported languages, and the migration needs to be tested independently on the internal services before external partners are contacted.", T),
]

# --- TRUE: each + 2+ keywords --------------------------------------------------
T_EACH_KW = [
    rec("Each service must adopt the new tracing headers, and the rollout spans multiple teams and several regions.", T),
    rec("Configure each adapter with its own timeout, and verify the failover independently across both clusters.", T),
    rec("Each environment needs its own secret set, and the rotation runs independently of the other components.", T),
    rec("Wire each provider through the same interface, and let the transports run in parallel behind the facade across multiple deployment modes.", T),
    rec("Each replica must process its own shard, and the rebalancing happens independently on the several nodes.", T),
    rec("Sanitize each request against the shared rules, and validate both the payload and the headers separately.", T),
    rec("Each module publishes its own metrics, and the collector aggregates them across the several services.", T),
    rec("Each tenant gets an isolated quota, and the enforcement runs independently in both the API and the worker.", T),
]

# --- TRUE: each + 3+ sentences -------------------------------------------------
T_EACH_SENT = [
    rec("Each region gets its own failover cluster. The read replicas are promoted independently. The traffic cutover follows the same runbook.", T),
    rec("Each exporter writes to its own sink. The retry policies differ per source. The dashboards refresh on a separate schedule.", T),
    rec("Each workflow step records its own audit trail. The approvals are tracked per role. The archive job runs on the first of the month.", T),
    rec("Each config file is validated before the deploy. The secrets are injected at runtime. The rollback restores the previous version.", T),
    rec("Each payment region settles in its own currency. The ledger entries are normalized before aggregation. The reconciliation runs at the close of the day.", T),
    rec("Each test suite runs in its own container. The coverage reports merge after the run. The flaky tests are quarantined automatically.", T),
]

# --- TRUE: each + length -------------------------------------------------------
T_EACH_LEN = [
    rec("Each document that arrives through the ingestion pipeline must be routed to the correct tenant partition, deduplicated against the existing corpus, normalized into the canonical schema, and indexed with the appropriate retention policy, and the processing chain has to be monitored continuously so that backlogs are caught before they affect the search quality on the public site.", T),
]

# --- TRUE: 3+ file refs + 2+ keywords ------------------------------------------
T_FILES_KW = [
    rec("Add the new endpoints to api.py, update the models in models.py, and extend the tests in test_api.py, and roll this out across both the staging and the production in parallel.", T),
    rec("Update main.py, settings.py, and the deploy script in deploy.sh to load the new config, and verify the change independently against the several environments.", T),
    rec("Refactor the handlers in app.py, the helpers in utils.py, and the client in client.py, and run the suites in parallel for both the sync and the async paths.", T),
    rec("Migrate the schema in db/migrate.sql, the seeds in db/seed.sql, and the mocks in tests/mocks.py, and review the changes separately with the multiple owners.", T),
    rec("Split the configs in dev.yaml, prod.yaml, and the base config in base.yaml, and load the layers independently for the several runtime modes.", T),
    rec("Rewrite the CLI in cli.py, the server in server.py, and the docs in docs.md, and ship the components together in the next release across both channels.", T),
    rec("Update the jobs in jobs.go, the workers in workers.go, and the queue in queue.go, and coordinate the rollout across the multiple clusters in parallel.", T),
    rec("Port the bindings in bindings.rs, the core in core.rs, and the tests in tests.rs, and validate the port separately on the several target platforms.", T),
]

# --- TRUE: 3+ file refs + each -------------------------------------------------
T_FILES_EACH = [
    rec("Each module now lives in its own file, so update index.ts, worker.ts, and shared.ts to the new layout.", T),
    rec("Compile each crate with the new flags, and adjust the settings in Cargo.toml, the scripts in build.sh, and the profile in profiles.json.", T),
    rec("Each service writes its trace to trace.json, its metrics to metrics.json, and its logs to logs.json.", T),
    rec("Sign each artifact with its own key, and record the hashes in checksums.json, the receipts in receipts.json, and the logs in audit.json.", T),
    rec("Bundle each shard separately, and update the maps in manifests.json, the sizes in sizes.json, and the checks in checks.json.", T),
]

# --- TRUE: 3+ file refs + 3+ sentences -----------------------------------------
T_FILES_SENT = [
    rec("The auth flow changes touch auth.py. The session store lives in sessions.py. The token refresh moves to refresh.py.", T),
    rec("The parser is defined in parser.py. The formatter lives in format.py. The entry point sits in main.py.", T),
    rec("The event schema is versioned in schema.json. The producer reads it in producer.py. The consumer validates it in consumer.py.", T),
    rec("The new decorators go in decorators.py. The helpers move to helpers.py. The tests update in test_core.py.", T),
]

# --- TRUE: 3+ file refs + exactly-2 verbs --------------------------------------
T_FILES_2VERBS = [
    rec("Add the retry hooks in client.py, update the timeouts in config.py, and document the change in README.md.", T),
    rec("Implement the cache in cache.py, refactor the loader in loader.py, and keep the interface in api.py stable.", T),
    rec("Build the exporter in exporter.py, fix the collector in collector.py, and review the wiring in run.py.", T),
    rec("Create the feature in feature.py, migrate the data in migrate.sql, and update the version in pyproject.toml.", T),
]

# --- TRUE: 3+ file refs + length ----------------------------------------------
T_FILES_LEN = [
    rec("The refactor spans main.py, app.py, and the supporting helpers in utils.py, and it also touches the migration scripts in db/seed.sql and the configuration in settings.toml, and the whole change needs to be tested against the previous release before it is merged into the main branch ahead of the quarterly freeze.", T),
    rec("The upgrade covers auth.py, session.py, and the middleware in middleware.py, and it requires coordination with the certificate rotation in certs.py, the deployment templates in deploy.yaml, and the runbook in runbook.md, and the affected teams need to be notified before the change is applied to the shared environment.", T),
    rec("The new monitoring setup wires alerts.py, the dashboards in dashboards.json, and the exporters in exporters.py, and it also updates the retention rules in retention.py and the notification routes in routes.py, and the validation has to run in the staging environment for a full week before the production rollout.", T),
]

# --- TRUE: itemized list alone -------------------------------------------------
T_LIST = [
    rec("Ship the feature in stages: 1) add the config flag 2) implement the core logic 3) expose the API endpoint 4) update the client SDK.", T),
    rec("Roll out the change as: 1) migrate the schema 2) backfill the existing rows 3) flip the read path 4) monitor the error rate.", T),
    rec("The cutover plan is: 1) freeze the source 2) sync the delta 3) verify the checksums 4) switch the DNS record.", T),
    rec("Prepare the release with these steps: 1) bump the version 2) regenerate the lockfile 3) run the full test suite 4) tag the commit.", T),
    rec("The hardening pass covers: 1) rotate the keys 2) patch the dependencies 3) tighten the network policy 4) rerun the scanner.", T),
    rec("Stage the upgrade like this: 1) snapshot the volumes 2) drain the nodes 3) apply the manifests 4) verify the health checks.", T),
    rec("Execute the rollout in order: 1) deploy the canary 2) watch the metrics 3) widen the traffic split 4) complete the rollout.", T),
    rec("Handle the incident as follows: 1) open the bridge 2) capture the logs 3) apply the mitigation 4) write the postmortem.", T),
]

# --- TRUE: itemized list + extra ----------------------------------------------
T_LIST_EXTRA = [
    rec("Complete the checklist for the new region: 1) provision the clusters 2) connect the peered networks 3) deploy the base services 4) verify the failover, and run each step in parallel where possible.", T),
    rec("Plan the maintenance window: 1) announce the freeze 2) run the backups 3) apply the patches 4) validate the cluster, and schedule the steps separately across the several zones.", T),
    rec("Follow the deployment procedure: 1) build the image 2) scan the image 3) push to the registry 4) deploy to staging 5) promote to production, and notify the teams once each step finishes.", T),
]

# --- TRUE: 3+ verb-led workstreams --------------------------------------------
T_3VERBS = [
    rec("Add the new checkout flow, update the tax calculator, migrate the order status enum, and backfill the pending orders.", T),
    rec("Refactor the auth middleware, add the session revocation endpoint, remove the legacy tokens, and update the client docs.", T),
    rec("Implement the data export job, update the retention policy, migrate the cold storage buckets, and backfill the missing archives.", T),
    rec("Build the notification router, add the delivery receipts, rewrite the failure handler, and update the templates.", T),
    rec("Create the rate limit store, update the proxy config, migrate the token buckets, and backfill the throttle history.", T),
    rec("Fix the webhook dedupe, add the replay endpoint, rewrite the signature verification, and update the subscription model.", T),
    rec("Introduce the tenant scoping, update the query builder, migrate the multi-tenant tables, and backfill the tenant ids.", T),
    rec("Port the search client, add the query fallbacks, rewrite the result ranking, and update the connector configs.", T),
    rec("Split the worker pool, add the priority queues, migrate the dead-letter buckets, and update the dispatch rules.", T),
]

# --- TRUE: exactly-2 verbs + 2+ keywords ---------------------------------------
T_2VERBS_KW = [
    rec("Add the idempotency key to the checkout API, update the retry middleware, and keep the changes compatible with the multiple client versions across both transports.", T),
    rec("Migrate the session store, update the token format, and keep the refresh flow working for the several existing apps across both platforms.", T),
    rec("Refactor the cache layer, add the eviction hooks, and preserve the behavior for the multiple callers that run in parallel.", T),
    rec("Update the billing cycle, migrate the proration logic, and keep the invoice numbers stable across the several tenants and both subscription tiers.", T),
    rec("Rewrite the exporter, add the backfill mode, and keep the output format identical for the multiple downstream tools and the several dashboards.", T),
    rec("Split the ingestion service, update the partitioner, and preserve the offsets across the multiple replicas in both zones.", T),
    rec("Create the plugin API, add the compatibility shims, and keep the extensions working for the several versions across both runtimes.", T),
    rec("Build the new dashboard, update the data source, and keep the charts stable for the multiple teams and the several owners.", T),
]

# --- TRUE: exactly-2 verbs + each ----------------------------------------------
T_2VERBS_EACH = [
    rec("Add the retry policy, update the backoff schedule, and keep each attempt idempotent.", T),
    rec("Create the batch processor, update the checkpoint logic, and persist each result before acking it.", T),
    rec("Implement the queue reader, migrate the message format, and validate each payload against the schema.", T),
    rec("Create the job runner, update the progress tracking, and emit each status change to the log.", T),
]

# --- TRUE: exactly-2 verbs + 3+ sentences --------------------------------------
T_2VERBS_SENT = [
    rec("Update the admin console, add the new user roles, and refresh the settings page. The permission matrix is documented in the wiki. The access groups are listed in the admin guide.", T),
    rec("Refactor the billing service, migrate the ledger table, and clean up the dead code. The invoice renderer stays unchanged. The statement exporter runs on the old path.", T),
    rec("Introduce the health endpoint, add the metrics route, and expose the readiness probe. The startup sequence logs the new checks. The pod selector is updated in the manifest.", T),
    rec("Update the onboarding checklist, add the new review step, and refresh the compliance docs. The auditor role is documented in the guide. The training record is checked at promotion.", T),
]

# --- TRUE: exactly-2 verbs + length --------------------------------------------
T_2VERBS_LEN = [
    rec("Update the onboarding flow so that the invitation emails, the identity verification steps, the team provisioning calls, the role assignments, and the audit trail entries are handled consistently across the platform, and add the progress tracking that lets the support team see exactly where each new user is stuck in the funnel before escalation is needed.", T),
]

TRAIN_PROFILES = [
    ("F_ATOMIC", F_ATOMIC),
    ("F_KEYWORDS", F_KEYWORDS),
    ("F_SENTENCES", F_SENTENCES),
    ("F_LENGTH", F_LENGTH),
    ("F_EACH", F_EACH),
    ("F_FILES", F_FILES),
    ("F_2VERBS", F_2VERBS),
    ("F_CONTEXT", F_CONTEXT),
    ("F_HARD", F_HARD),
    ("T_SENT_KW", T_SENT_KW),
    ("T_SENT_LEN", T_SENT_LEN),
    ("T_LEN_KW", T_LEN_KW),
    ("T_EACH_KW", T_EACH_KW),
    ("T_EACH_SENT", T_EACH_SENT),
    ("T_EACH_LEN", T_EACH_LEN),
    ("T_FILES_KW", T_FILES_KW),
    ("T_FILES_EACH", T_FILES_EACH),
    ("T_FILES_SENT", T_FILES_SENT),
    ("T_FILES_2VERBS", T_FILES_2VERBS),
    ("T_FILES_LEN", T_FILES_LEN),
    ("T_LIST", T_LIST),
    ("T_LIST_EXTRA", T_LIST_EXTRA),
    ("T_3VERBS", T_3VERBS),
    ("T_2VERBS_KW", T_2VERBS_KW),
    ("T_2VERBS_EACH", T_2VERBS_EACH),
    ("T_2VERBS_SENT", T_2VERBS_SENT),
    ("T_2VERBS_LEN", T_2VERBS_LEN),
]

# ---------------------------------------------------------------------------
# EVAL (held-out, disjoint from train)
# ---------------------------------------------------------------------------

EVAL_TASKS = [
    # false
    rec("Add the missing semicolon to the minified bundle config.", F),
    rec("Fix the timeout in the health check request.", F),
    rec("Rename the `env` field to `environment` in the payload.", F),
    rec("Update the favicon across the marketing pages.", F),
    rec("Lower the log verbosity for the debug channel.", F),
    rec("Restart the crawler after the index corruption warning.", F),
    rec("Remove the unused import from the trace decoder.", F),
    rec("Set the default branch name for new repositories.", F),
    rec("The scheduled report is missing the revenue column. The summary table drops the currency field. The pivot view ignores the date filter.", F, note="decoy: 3 sentences, evidence 1"),
    rec("The client shows the wrong error message. The server returns a generic 500. The logging path hides the original cause.", F, note="decoy: 3 sentences, evidence 1"),
    rec("The backup job finishes early on Fridays. The retention window is misconfigured. The snapshot list does not include the weekly full.", F, note="decoy: 3 sentences, evidence 1"),
    rec("Coordinate the demo with the several stakeholders and both marketing teams.", F, note="decoy: 2 keywords, evidence 1"),
    rec("Summarize the incident for the multiple channels and the several on-call groups.", F, note="decoy: 2 keywords, evidence 1"),
    rec("The change touches multiple layers and several endpoints, but it is a one-line constant tweak.", F, note="hard: 2 keywords, evidence 1"),
    rec("Document the several rate limits for both the public and the internal APIs.", F, note="decoy: 2 keywords, evidence 1"),
    rec("Normalize each amount before the currency conversion.", F, note="decoy: 'each', evidence 1"),
    rec("Compress each shard before the archival upload.", F, note="decoy: 'each', evidence 1"),
    rec("Update the stubs in api.py, client.py, and models.py to the latest interfaces.", F, note="decoy: 3 file refs, evidence 1"),
    rec("Refresh the manifests in dev.yaml, prod.yaml, and test.yaml.", F, note="decoy: 3 file refs, evidence 1"),
    rec("Fix the quoting in query.py, seed.sql, and the docs in runbook.md.", F, note="decoy: 3 file refs, evidence 1"),
    rec("Add the request logging, update the response headers, and rerun the perf suite.", F, note="decoy: 2 verb-led clauses, evidence 1"),
    rec("Create the dev profile, refactor the bootstrap, and smoke-test the local setup.", F, note="decoy: 2 verb-led clauses, evidence 1"),
    rec("Add the webhook signature check, add the retry for the delivery failures, and add the alert on repeated rejects.", F, context="The subtask plan for this change is in the tracker."),
    rec("Update the cache invalidation, update the replica lag monitor, and update the failover script.", F, context="A decomposition of this task already exists."),
    rec("The provisioning script is slow on Mondays. The retry loop re-fetches the whole catalog. The fix is a cache key in the client.", F, note="hard: 3 sentences, evidence 1"),
    # true
    rec("Add rate limiting to the gateway. The quota pools are configured per tenant. The rollout proceeds independently across the several zones.", T),
    rec("Update the scheduler, update the worker pool, update the retry policy, and refresh the runbook.", T),
    rec("Migrate the auth service, add the refresh token rotation, rewrite the session store, and update the client docs.", T),
    rec("Each shard must be rebalanced independently, and the reassignment runs in parallel across both regions.", T),
    rec("Each tenant needs its own approval flow. The reviewers are assigned per role. The audit log records every decision.", T),
    rec("Implement the plugin hooks, update the manifest loader, and validate each addon before activation.", T),
    rec("Ship the release: 1) freeze the feature set 2) run the regression suite 3) sign the artifacts 4) publish the notes.", T),
    rec("The cutover checklist is: 1) verify the backups 2) disable the writes 3) sync the replicas 4) enable the writes 5) confirm the lag.", T),
    rec("Port the telemetry client, add the metric forwarding, and keep the wire format unchanged for the several consumers and both exporters.", T),
    rec("The migration touches migrations.py, the models in models.py, and the tests in test_db.py, and the rollout needs to be rehearsed separately across multiple regions.", T),
    rec("Each region maintains its own failover set. The promotion steps are documented per site. The drills run on a quarterly schedule.", T),
    rec("Refactor the ledger reader, add the idempotent writes, migrate the stale partitions, and verify the change against the several snapshot formats.", T),
    rec("Update the API gateway, update the auth filter, update the rate limit store, and test the change across both environments.", T),
    rec("Introduce the new permission model, migrate the role bindings, backfill the policy cache, and document the change in the runbook.", T),
    rec("The refactor covers core.py, the adapters in adapters.py, and the harness in test_harness.py, and it must land without breaking the multiple integrations and the several plugins.", T),
    rec("Each event is fanned out to its own queue. The consumers acknowledge independently. The dead-letter retries run on a separate schedule.", T),
    rec("Add the tenant isolation check, update the query planner, and verify each data access path.", T),
    rec("Migrate the billing tables, update the invoice generator, backfill the proration records, and keep the reports identical for the existing clients.", T),
    rec("Create the canary config, update the error budget dashboard, split the traffic gradually, and port the settings to the new config.", T),
    rec("Create the backup scheduler, add the retention rules, and rewrite the restore flow, and test each step against the staging environment.", T),
    rec("Each billing cycle needs its own cutoff report. The adjustments are tracked per account. The closing entries run after the final sync.", T),
    rec("The review covers auth.py, the session store in sessions.py, and the token refresh in refresh.py, and the remediation is tracked independently across the teams in both regions.", T),
    rec("Split the import pipeline, add the checkpoint restart, rewrite the retry loop, and validate the change against the previous year's data.", T),
    rec("Update the search indexer, migrate the document store, backfill the re-ranked results, and run the comparison across the several query sets.", T),
    rec("Add the feature flag service, update the rollout dashboard, split the traffic by cohort, and monitor the results across the multiple regions.", T),
]

# ---------------------------------------------------------------------------
# CANARIES (adversarial traps; labels still rule-engine-consistent)
# ---------------------------------------------------------------------------

CANARY_TASKS = [
    {
        "task": "Roll out the new dashboard to the several staging services and then promote to production. This touches multiple components, both the API and the worker, and involves many independent settings to verify.",
        "label": False,
        "context": "",
        "canary": {
            "name": "keyword-dense dashboard rollout",
            "kind": "trivially_atomic",
            "anti_expected": True,
            "anti_expected_confidence_range": [0.5, 1.0],
            "failure_mode": "over_decomposition: a keyword-counting model splits an atomic rollout",
            "description": "Four HIGH_SIGNAL keywords (several, multiple, both, components) but evidence sum is 1; gold is false.",
        },
    },
    {
        "task": "Update the webhook dispatcher to retry with jitter, add circuit breaking to the event consumer, rewrite the dead-letter handler, and refresh the operational runbook.",
        "label": True,
        "context": "",
        "canary": {
            "name": "verb-led dispatcher upgrade",
            "kind": "must_decompose",
            "anti_expected": False,
            "anti_expected_confidence_range": [0.5, 1.0],
            "failure_mode": "under_decomposition: a keyword-blind model keeps a 3-workstream task whole",
            "description": "Zero HIGH_SIGNAL keywords, three verb-led workstreams; gold is true.",
        },
    },
    {
        "task": "Support both the web and the mobile clients by adding a shared config, and cover the several envs with the same job. This spans multiple services but is one feature: deploy both together.",
        "label": False,
        "context": "",
        "canary": {
            "name": "keyword-stuffed single feature",
            "kind": "keyword_hack",
            "anti_expected": True,
            "anti_expected_confidence_range": [0.5, 1.0],
            "failure_mode": "over_decomposition: keyword density alone cannot decide the label",
            "description": "both/several/multiple/services all present but the work is one feature; gold is false.",
        },
    },
    {
        "task": "The legacy importer needs a fix. It drops rows when the source file has a header. The fix is a one-line guard in the parser.",
        "label": False,
        "context": "",
        "canary": {
            "name": "three-sentence atomic fix",
            "kind": "ambiguous_calibration",
            "anti_expected": True,
            "anti_expected_confidence_range": [0.5, 1.0],
            "failure_mode": "over_decomposition: a sentence-counting baseline over-decomposes",
            "description": "Three sentences but evidence sum is 1; gold is false. Engine emits only fixed confidence tiers (0.7/0.8/0.9), so the spec's confidence <= 0.6 pass condition is approximated by this competing-heuristic case.",
        },
    },
    {
        "task": "Add retries to the billing client, update the payment docs, and finish with a single deployment.",
        "label": False,
        "context": "",
        "canary": {
            "name": "duplicate-looking atomic (pair A)",
            "kind": "near_duplicate_contradiction",
            "anti_expected": True,
            "anti_expected_confidence_range": [0.5, 1.0],
            "failure_mode": "memorization: pattern similarity to the true member must not flip this label",
            "description": "Two verb-led clauses only (evidence 1); gold is false. Pairs with the next canary record, which opens identically.",
        },
    },
    {
        "task": "Add retries to the billing client, update the payment docs, migrate the ledger schema, and backfill the audit trail.",
        "label": True,
        "context": "",
        "canary": {
            "name": "duplicate-looking decomposed (pair B)",
            "kind": "near_duplicate_contradiction",
            "anti_expected": False,
            "anti_expected_confidence_range": [0.5, 1.0],
            "failure_mode": "memorization: same opening as the false member but three verb-led workstreams",
            "description": "Three verb-led workstreams (evidence 2); gold is true. A model that learns similarity rather than evidence flips one of the pair.",
        },
    },
    {
        "task": "Add rate limiting to the public API, add retry logic to the workers, and add a migration for the throttling table.",
        "label": False,
        "context": "Subtask list: 1) rate limiting 2) retry logic 3) migration.",
        "canary": {
            "name": "context already decomposed",
            "kind": "context_suppression",
            "anti_expected": True,
            "anti_expected_confidence_range": [0.5, 1.0],
            "failure_mode": "context-blind model over-decomposes despite an explicit prior decomposition",
            "description": "The task alone would decompose (two verb-led workstreams); the context already names subtasks, so the engine short-circuits to false.",
        },
    },
    {
        "task": "Investigate and document why the nightly reconciliation job occasionally produces duplicate ledger entries by tracing the job's SQL through the batching layer, the dedupe key construction, the materialized view refresh, the idempotency token generation, and the retry logic that re-enqueues failed batches before the audit window closes on the last business day of the quarter.",
        "label": False,
        "context": "",
        "canary": {
            "name": "long but atomic investigation",
            "kind": "trivially_atomic",
            "anti_expected": True,
            "anti_expected_confidence_range": [0.5, 1.0],
            "failure_mode": "over_decomposition: a length heuristic (>220 chars) over-decomposes",
            "description": "Long single-requirement task; length is the only evidence (+1), so gold is false.",
        },
    },
    {
        "task": "Rewrite the auth flow to the new library. The change is one module, several function signatures, and a single migration. Keep it as one commit.",
        "label": False,
        "context": "",
        "canary": {
            "name": "format-only rationale trap",
            "kind": "format_only_hack",
            "anti_expected": True,
            "anti_expected_confidence_range": [0.5, 1.0],
            "failure_mode": "rationale-gaming: a filler or length-only rationale passes a future rationale check",
            "description": "Three sentences and a 'several' bait keyword, but evidence sum is 1; gold is false and the rationale_keywords force citing atomicity. Pass condition approximated via rationale_keywords because the v2 metric does not score rationale length.",
        },
    },
    {
        "task": "Migrate the storage layer in stages: 1) add the new blob layout 2) write the backfill job 3) swap the read path 4) enable the write path.",
        "label": True,
        "context": "",
        "canary": {
            "name": "itemized no-keyword migration",
            "kind": "must_decompose",
            "anti_expected": False,
            "anti_expected_confidence_range": [0.5, 1.0],
            "failure_mode": "under_decomposition: itemized-list evidence is ignored",
            "description": "Four numbered items, zero keywords, one sentence; gold is true via the itemized-list signal (+2).",
        },
    },
]

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def rationale_keywords(reason: str) -> list[str]:
    if reason == "task is atomic or already scoped":
        return ["atomic", "scoped"]
    if reason == "context already provides a decomposition":
        return ["context", "already", "decomposed"]
    return [frag for frag in reason.split("; ") if frag]


def neutral_decide(task: str, context: str) -> dict[str, object]:
    """Run one candidate through the owning module's neutral CLI."""
    output = run_module_cli(
        MANIFEST.cli_module,
        {"task": task, "context": context},
        cwd=ROOT,
        source_root=ROOT / "src",
    )
    if not isinstance(output.get("decompose"), bool):
        raise SystemExit("module CLI returned no boolean 'decompose' decision")
    if not isinstance(output.get("reason"), str):
        raise SystemExit("module CLI returned no string 'reason'")
    confidence = output.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise SystemExit("module CLI returned no numeric 'confidence'")
    return output


def build_record(
    task: str, context: str, label: bool, split: str, rid: str, note: str, source: str, canary: dict | None = None
) -> dict:
    out = neutral_decide(task, context)
    if out["decompose"] != label:
        raise SystemExit(
            f"MISMATCH: id={rid} split={split} intended={label} "
            f"engine={out['decompose']}\n"
            f"  task: {task}\n  context: {context}\n  reason: {out['reason']}"
        )
    record = {
        "id": rid,
        "schema_version": SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "split": split,
        "added_at": ADDED_AT,
        "added_by": ADDED_BY,
        "source": source,
        "license": "internal",
        "redacted": False,
        "input": {"task": task, "context": context},
        "expected": {"decompose": out["decompose"], "reason": out["reason"]},
        "expected_confidence": out["confidence"],
        "rationale_keywords": rationale_keywords(out["reason"]),
        "notes": note,
    }
    if canary is not None:
        record["canary"] = True
        record["canary_info"] = canary
    return record


def emit(records: list[dict], path: Path) -> None:
    records.sort(key=lambda r: r["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    print(f"wrote {len(records)} records -> {path}")


def main() -> int:
    train: list[dict] = []
    seen_ids: set[str] = set()

    def unique(rid: str) -> str:
        assert rid not in seen_ids, f"duplicate id {rid}"
        seen_ids.add(rid)
        return rid

    for i, entry in enumerate([r for _, prof in TRAIN_PROFILES for r in prof], start=1):
        train.append(
            build_record(
                entry["task"], entry["context"], entry["label"], "train",
                unique(f"should_decompose-{i:04d}"), entry["note"], "hand-authored",
            )
        )

    eval_records: list[dict] = []
    for i, entry in enumerate(EVAL_TASKS, start=201):
        eval_records.append(
            build_record(
                entry["task"], entry["context"], entry["label"], "eval",
                unique(f"should_decompose-{i:04d}"), entry["note"], "hand-authored",
            )
        )

    canary_records: list[dict] = []
    for i, entry in enumerate(CANARY_TASKS, start=1):
        canary_records.append(
            build_record(
                entry["task"], entry["context"], entry["label"], "canary",
                unique(f"should_decompose-canary-{i:02d}"), "", "hand-authored", entry["canary"],
            )
        )

    if MANIFEST is None:
        print(
            f"no module with manifest module_name={DEFAULT_LOGICAL_MODULE!r} "
            "found; nothing to generate"
        )
        return 0

    datasets = MANIFEST.package_dir / "datasets"
    emit(train, datasets / "train.jsonl")
    emit(eval_records, datasets / "eval.jsonl")
    emit(canary_records, datasets / "canaries.jsonl")

    meta = {
        "schema_version": SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "eval_frozen_at": ADDED_AT,
        "canary_frozen_at": ADDED_AT,
        "sibling_pins": {},
    }
    (datasets / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"wrote {datasets / 'meta.json'}")

    n_true = sum(1 for r in train if r["expected"]["decompose"])
    print(f"train: {len(train)} records, decompose=true {n_true}, false {len(train) - n_true}")
    n_true_e = sum(1 for r in eval_records if r["expected"]["decompose"])
    print(f"eval: {len(eval_records)} records, decompose=true {n_true_e}, false {len(eval_records) - n_true_e}")
    n_true_c = sum(1 for r in canary_records if r["expected"]["decompose"])
    print(f"canaries: {len(canary_records)} records, decompose=true {n_true_c}, false {len(canary_records) - n_true_c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
