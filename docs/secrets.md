# Placeholder secrets

A workspace never holds a real secret. It holds a **placeholder** —
a sentinel token (`mskssec1_…`) the operator mints once and pastes
into the workspace — and the daemon swaps the sentinel for the real
secret on the wire, in flight, only toward the destinations the
mint named. The real secret lives in msksd's **secret store** and in
the daemon's memory, nowhere else.

This chapter is about the store and the mint/revoke/renew commands.
The in-flight swap itself is the egress interceptor
([#194 decision doc](spikes/194-egress-interceptor.md); the
implementation lands with the interceptor).

## The mint flow

```console
$ op read 'op://Vault/github/credential' \
    | msks secret mint myws --name github_api \
        --dest api.github.com --secret-file -
minted myws/github_api for api.github.com
sentinel (shown once): mskssec1_9Jm3...kQ
```

- `--secret-file` takes a path, or `-` to read the secret from a
  pipe. The secret is never accepted as a command-line argument:
  arguments land in process lists and shell history.
- `--dest` repeats: an exact host (`api.github.com`) binds the swap
  to that host; a suffix (`.github.com`) binds it to every host
  under that domain. A placeholder carries one workspace, one
  allowlist, one secret; the same secret in N workspaces is N mints.
- `--ttl SECONDS` gives the placeholder a lifetime; without it the
  placeholder lives until revoked. `msks secret renew` extends a
  lifetime in place — the sentinel never changes and nothing is
  re-delivered.
- The sentinel is printed once, at mint. Every later view (list,
  audit, logs) omits it; a lost sentinel is re-minted, not recalled.

`msks secret revoke myws --name github_api` takes effect on the
next request. `msks secret check` verifies the configured store
answers writes before the first mint — a typo'd setting fails there,
loudly.

## Where the real secret lives

The store is [SecretSpec](https://secretspec.dev) driven through its
CLI (`secretspec`, named by `secret_store_cli`). The **provider is a
setting** — where the bytes live is a configuration choice, not
code, so moving from a local file to at-rest encryption or a managed
vault changes no msks code:

| Provider | Setting value    | Where the secret lives                                                                        | The credential the daemon holds                                                                               |
| -------- | ---------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `file`   | `file` (default) | One file per secret under the store root (`<state_dir>/secrets`), filesystem permissions only | none                                                                                                          |
| `age`    | `age`            | One age-encrypted file under the store root                                                   | an age identity file in the state dir                                                                         |
| `awssm`  | `awssm`          | AWS Secrets Manager, under the `secretspec/msks/` name prefix                                 | the AWS SDK credential chain — on AWS-hosted appliances an instance profile role, no stored credential at all |
| `bws`    | `bws`            | A Bitwarden Secrets Manager project                                                           | a machine-account access token (`BWS_ACCESS_TOKEN`) in the daemon's environment                               |

Plain Bitwarden Password Manager (`bw`) keeps its session only
while an operator holds it unlocked, so it serves interactive use;
an unattended daemon uses `bws`.

Provider credentials are deliberately **not msksd settings**: the
`secretspec` subprocess inherits the daemon's environment, so each
provider resolves its own chain (the AWS SDK chain, `BWS_ACCESS_TOKEN`).
Nothing doubles through msksd's configuration.

### Worked examples

`file` — the default; one file per secret under the state dir:

```yaml
secret_store_provider: file
# secret_store_root defaults to <state_dir>/secrets
```

`age` — at-rest encryption of the store (protects copies of the
state dir; the identity file itself is the appliance's one bootstrap
secret):

```yaml
secret_store_provider: age
secret_store_age_identity: /var/lib/msksd/secrets/age.key
# generate once: age-keygen -o /var/lib/msksd/secrets/age.key
```

`awssm` — secrets in AWS; an instance profile role scopes to the
`secretspec/msks/*` names and no credential is stored on the host:

```yaml
secret_store_provider: awssm
secret_store_region: eu-west-1
# optional: secret_store_profile and secret_store_prefix
```

`bws` — a Bitwarden Secrets Manager project, token via the daemon's
environment:

```yaml
secret_store_provider: bws
secret_store_project: 5f8a-...-project-uuid
# systemd unit: Environment=BWS_ACCESS_TOKEN=...
```

## At-rest encryption is a provider choice

The `file` provider stores plaintext bytes behind filesystem
permissions — the same posture as every other secret-bearing
artifact in the state dir (the database, the ssh identities). When
that is not enough, `age` encrypts the store in place and `awssm` /
`bws` move the bytes off the appliance entirely. All three are
settings; msksd never picks an algorithm itself.

## The audit trail

Mint, revoke, and expiry append a row to the appliance database:
the placeholder's workspace, name, destination allowlist, and a
timestamp. The secret value and the sentinel appear nowhere in the
audit — the value is not msks's to log, and the sentinel is never
shown past its single mint-time print. `msks secret ls` lists
placeholders; expiry fires from the status watcher's periodic sweep
and publishes a `secret.expiry` event on the events channel.
