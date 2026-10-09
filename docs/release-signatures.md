# Verifying release signatures

New releases built by the signed release pipeline provide:

- `inverter-control-X.Y.Z.tar.gz`, the native source and installer package;
- `tls-dependencies-cp312-armv7-X.Y.Z.tar.gz`, the optional dependency bundle for
  the specifically supported ARMv7 ABI;
- `SHA256SUMS` and `SHA256SUMS.sigstore.json`, authenticating both payloads;
- package/signature receipts and `release-manifest.json`, recording build and
  promotion provenance.

Historical releases without the signature bundle are unsigned. A successful
check of `SHA256SUMS` alone authenticates neither the publisher nor the release.
Do not select an unsigned historical release when a signature is required. The
feature is not evidence that a signed release has already been published; inspect
the actual selected release assets and verify them.

## Trust and verification

Install [Cosign 3.1.3 or later from Sigstore](https://docs.sigstore.dev/cosign/system_config/installation/)
on a trusted workstation. Obtain the expected reviewed source commit through
the project's reviewed change/release record, independently of the downloaded
manifest. A self-asserted source SHA in an unverified artifact is not a trust root.
The published trust policy is:

- OIDC issuer: `https://token.actions.githubusercontent.com`;
- workflow identity:
  `https://github.com/victron-venus/inverter-control/.github/workflows/release-build.yml@refs/heads/main`;
- certificate source repository: `victron-venus/inverter-control`;
- certificate source ref: `refs/heads/main`;
- certificate source commit: the expected reviewed source SHA.

These values are fixed; do not replace them with values copied out of an
untrusted certificate. Binding the source repository and ref prevents a fork
from reusing the public signing workflow to impersonate an official release. The workflow identity is the reusable signing workflow,
not the parent release orchestrator. Cosign's trusted Sigstore root authenticates
the Fulcio certificate and Rekor transparency evidence. The bundle includes the
short-lived public certificate and the transparency evidence; no project-private
signing key needs to be downloaded. See [Sigstore verification](https://docs.sigstore.dev/cosign/verifying/verify/).

Download the two tarballs, checksum manifest and signature bundle into an empty
directory. Using the verification helper from a trusted reviewed checkout:

```sh
python3 /path/to/trusted/inverter-control/scripts/release_signatures.py verify \
  /path/to/downloaded-assets --source-sha EXPECTED_40_CHARACTER_SOURCE_SHA
```

The auxiliary `release-inputs-*.json` and `release-manifest.json` files are not
covered by `SHA256SUMS`. They provide workflow provenance checked by the publisher,
but recipients must not use them as an independently authenticated trust source
or let them choose the expected signing identity/source SHA.

The helper first authenticates the checksum manifest with Cosign, then checks the
complete source/ARMv7 payload inventory, matching versions, safe filenames and
all payload hashes. It rejects symlinks and unexpected extra payload files.
Do not extract or run the downloaded package until verification succeeds.

For an independent manual signature check, use the same fixed policy:

```sh
cosign verify-blob --bundle SHA256SUMS.sigstore.json \
  --certificate-identity 'https://github.com/victron-venus/inverter-control/.github/workflows/release-build.yml@refs/heads/main' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  --certificate-github-workflow-repository 'victron-venus/inverter-control' \
  --certificate-github-workflow-ref 'refs/heads/main' \
  --certificate-github-workflow-sha EXPECTED_40_CHARACTER_SOURCE_SHA SHA256SUMS
# Only after successful authentication, inspect the two expected filenames:
sha256sum --check SHA256SUMS
```

Verify on the workstation before transferring bytes to an offline device. Retain
the verified checksums and check transfer integrity on the target; the device
need not install Cosign. Never disable transparency or certificate verification
to work around a failure. Availability of trust-root refresh/network access
belongs to the workstation verification step, not to the controller runtime.

## Signing and promotion

The compiler job has no OIDC signing permission. A separate ephemeral
GitHub-hosted signing job checks the original frozen-plan build receipt, obtains
a short-lived OIDC identity, signs `SHA256SUMS`, verifies the signature and payloads,
and records a second receipt for the signature bundle. No persistent signing key
is stored in the repository, Actions secrets, or the GitHub Release distribution
store. Private key material exists only in the ephemeral signing process.

The existing publisher requires exact receipt coverage of every uploaded asset
and verifies the uploaded draft bytes before publication. Stable promotion
copies the accepted RC payloads, signatures and original manifest byte-for-byte;
it does not rebuild or resign. Consequently verification uses the RC's reviewed
source commit, not the later promotion run's commit. This protects against
artifact replacement in the distribution store, not compromise of the reviewed
source, the signing workflow, GitHub's OIDC authority or Sigstore trust roots.
Tags remain lightweight pointers; this is artifact signing, not signed Git tags.

When updating vendored release tooling, run the repository wrapper against the
reviewed toolkit checkout:

```sh
uv run python scripts/regenerate_release.py /path/to/venus-os-ci-toolkit
uv run python scripts/release_signing_overlay.py --check
uv run python scripts/release_generation_overlay.py --check
```

The wrapper reapplies the minimal `id-token: write` permission needed by the
reusable signing job. It retains the reviewed harden-runner SHA and version from
the non-generated `release-build.yml` adapter and restores the
[required release-note format](release-notes-policy.md) in `RELEASING.md`.
It fails if the generator changes `.release-policy.json`, including its
`release_notes` hook or validation workflows. Tests fail if raw regeneration
removes an overlay or the expected template structure changes. Review regenerated
diffs and run the complete normal CI gate; other shared tooling changes still
require review.
Do not add signing permission to pull-request validators or compilation jobs.
