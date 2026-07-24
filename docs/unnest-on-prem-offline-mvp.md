# Unnest On-Prem Offline MVP

This document is the implementation contract for the first supported offline
Unnest deployment. It narrows the broader on-premise roadmap to one installable,
testable delivery profile.

## Definition of done

An SI developer exports one immutable Agent Flow, one immutable Ingestion Flow,
their immutable Subflow closure, and the SI-provided source documents. A
government administrator installs the resulting release on an internet-isolated
server, completes setup in a browser, and runs both safe and sandboxed flows
without transferring runtime data to the SI network.

The current layout-v2 checkpoint implements the signed safe-Flow package,
installer, and hash-locked Python wheel bundle. It deliberately blocks risky
Flows and bundled source documents until the later sandbox and document
checkpoints are implemented. Those blocked features remain part of this final
definition of done.

## Supported profile

| Area | MVP decision |
| --- | --- |
| Host | Rocky Linux 9.x, AMD64 |
| Orchestrator | Docker Compose on one server |
| Prerequisite | Docker Engine and the Docker Compose plugin are preinstalled |
| Database | Bundled PostgreSQL with a local persistent volume |
| Queue and quota store | Bundled Redis with a local persistent volume |
| File and index storage | Local persistent volumes |
| Model | Government-internal model endpoint configured during setup |
| Transport | HTTPS on the server IP and port 7860 |
| Initial TLS | Installer-generated self-signed certificate |
| Production TLS | Optional institution certificate and private key |
| Package confidentiality | Not encrypted; Flow JSON and documents remain plaintext inside the package |
| Package integrity | SHA-256 checksums and an SI release signature are required |
| Backup | Local encrypted backup, download, verification, and restore |

The following are not supported by this MVP:

- Podman, Kubernetes, Helm, HA, and ARM64
- installation of Docker itself
- bundled model servers or model weights
- NFS or S3 storage and backup
- OS packages or external binaries declared by Custom Components
- automatic upgrade, rollback, or blue-green deployment
- package encryption
- Grafana bundles

## Delivery and trust model

The product vendor signs the offline license. Each SI company owns a separate
release signing key and can export releases without vendor approval.

The SI public key is enrolled once through a separate approved delivery channel:

```bash
unnestctl trust import si-release.pub
```

The trusted key is stored under `/etc/unnest/trust/`. A public key contained in a
release package must never establish trust. `unnestctl` verifies the release
against the previously enrolled key.

The release is distributed as one tar file. `unnestctl` extracts it into a
temporary directory using traversal-safe extraction, verifies the signed
checksum file before using package contents, and rejects missing, additional, or
modified required files.

`unnestctl` itself is installed through the separately approved vendor channel.
It is not copied from, or executed out of, the release tar because that would
make the package part of its own trust bootstrap.

## Package contract

```text
unnest-<release>-rocky9-amd64.tar
├── manifest/release.json
├── openapi/openapi.json
├── compose/compose.yml
├── flows/<flow-version-id>.json
├── wheels/requirements.lock
├── wheels/<locked-package>.whl
├── images/unnest-runtime.tar
├── images/postgresql.tar
├── images/redis.tar
├── reports/sbom.cdx.json
├── reports/sbom-postgresql.cdx.json
├── reports/sbom-redis.cdx.json
├── reports/trivy.json
├── tests/acceptance.json
├── license/license.json
├── license/license.sig
├── signatures/checksums.sig
└── checksums.sha256
```

`checksums.sha256` covers every release file except itself and its detached
signature. `signatures/checksums.sig` signs the exact bytes of
`checksums.sha256`. The manifest records every image digest, Flow Version and
digest, Python dependency and hash, document checksum, port, endpoint, secret
name, resource limit, acceptance test, and license requirement.

No install step may access the internet or an external container registry.

## Runtime composition

Docker Compose starts:

- the Runtime API and restricted Runtime UI;
- PostgreSQL;
- Redis.

Safe Flow execution currently runs in the Runtime process, which is the only
executor actually connected to the Runtime API. Export validation blocks risky
Flows instead of packaging the existing sandbox client stub. A sandbox worker
and allowlist proxy are added only after their server protocol and isolation
tests exist; that checkpoint will increment the package layout.

The same immutable release snapshot is used by REST, streaming, web chat,
webhook, and cron execution.

The Runtime profile does not expose the Flow editor, Component or Bundle
management, Store, templates, community features, or deployment creation APIs.
External telemetry is disabled in the image.

## Custom Python dependencies

Components may declare Python packages only. Every package must have an exact
version and one or more wheel hashes in the immutable Flow declaration. The
Build Worker never downloads packages: it resolves every declared hash from the
operator-provisioned `UNNEST_OFFLINE_WHEELHOUSE`, writes a hash-locked
`wheels/requirements.lock`, and installs those same wheels into the Runtime
image with the package index disabled. The package verifier independently
checks the Flow declarations, manifest lock, wheel filenames, and wheel hashes.

Export is blocked when a Custom Component requires:

- an unpinned Python dependency;
- an unavailable wheel for Linux AMD64;
- an OS package; or
- an external binary.

## Sandbox contract

If the Agent Flow, Ingestion Flow, or a referenced Subflow contains custom code,
shell or subprocess execution, dynamic code execution, or arbitrary file-write
capability, the whole affected Flow runs in the sandbox worker. Components are
not serialized individually; only the Flow input and final result cross the
Runtime/sandbox boundary.

The sandbox must run as a non-root user with a read-only root filesystem,
dropped Linux capabilities, no privilege escalation, bounded CPU, memory, disk,
and execution time, and a disposable writable workspace.

Sandbox network access is denied by default. Only government-internal endpoints
declared in the immutable release manifest are reachable through the egress
proxy. Internet access and undeclared internal addresses are blocked.

## Bundled documents

SI-provided source documents are always included under `documents/source/`.
Prebuilt indexes are not included in the MVP. After the government-internal
model endpoint is configured, the Runtime builds a new local index from the
source documents and exposes progress and item-level errors.

The Agent API remains unavailable until all required source documents are
indexed and required acceptance tests pass.

## First-run setup

After installation, `unnestctl` prints:

```text
https://<server-ip>:7860/setup
```

Before setup completes, Agent, webhook, and schedule execution return not-ready
responses. The setup UI collects:

1. the first local administrator account;
2. the government-internal model endpoint;
3. secret values named by the release manifest;
4. an optional institution TLS certificate and key; and
5. confirmation that the one-time backup recovery identity was downloaded.

Setup then starts bundled-document indexing and required acceptance tests. The
Agent API becomes ready only after both succeed.

## Installer commands

The MVP CLI surface is:

```text
unnestctl trust import <public-key>
unnestctl verify <release.tar>
unnestctl preflight <release.tar>
unnestctl install <release.tar>
unnestctl status
unnestctl acceptance
unnestctl backup
unnestctl restore <backup>
```

`preflight` verifies Rocky Linux 9.x, AMD64, Docker Engine, Docker Compose, CPU,
memory, disk, required ports, the government-internal endpoints, checksums,
signature, and license. `install` loads every bundled image and must never pull.

## Offline acceptance test

The MVP is complete only when an automated test on a fresh, internet-isolated
Rocky Linux 9.x AMD64 host proves all of the following:

1. the SI public key can be enrolled;
2. a valid release installs with no registry or internet access;
3. tampered content, checksum, signature, or license blocks installation;
4. setup is the only available workflow before activation;
5. self-signed HTTPS and optional institution TLS work by server IP;
6. the first administrator, model endpoint, and secrets are stored securely;
7. all bundled source documents are indexed locally;
8. safe flows execute in the standard worker;
9. risky flows execute in the sandbox worker;
10. sandbox filesystem, privilege, resource, and endpoint restrictions hold;
11. REST, streaming, web chat, webhook, and cron execute the immutable Agent Version;
12. uploaded documents become searchable and original downloads remain admin-only;
13. encrypted backup and restore on another server preserve users, data, index, configuration, and audit state;
14. containers and the host can restart without data loss; and
15. no external telemetry connection is attempted.

Mocked command tests do not satisfy this acceptance test.

## Implementation checkpoints and repository skills

Each checkpoint must be independently runnable and committed before the next
begins.

1. **Package contract and assembler**
   - Generate the exact package layout from the existing image artifact.
   - Apply `.agents/skills/backend-code-review` before commit.
2. **Trust store, safe extraction, Compose, and install**
   - Make `unnestctl install <release.tar>` work end to end.
   - Apply `backend-code-review` and `.agents/skills/e2e-testing`.
3. **Locked wheel bundle and source documents**
   - Install only the wheels and documents recorded in the manifest.
   - Apply `backend-code-review` and `e2e-testing`.
4. **Sandbox worker and allowlist proxy**
   - Prove isolation with adversarial tests.
   - Apply `backend-code-review` and `e2e-testing`.
5. **Wizard and Runtime UI completion**
   - Use `.agents/skills/frontend-query-mutation` for API hooks.
   - Apply `.agents/skills/component-refactoring` before extending existing React files over 300 lines.
   - Apply `.agents/skills/frontend-testing` and `.agents/skills/frontend-code-review` before commit.
6. **Air-gapped acceptance**
   - Apply `e2e-testing` and retain the executable Rocky Linux test harness and results.
