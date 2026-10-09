# Project governance

Inverter Control uses a maintainer-led model. The project owner, [4alvit](https://github.com/4alvit), is accountable for its direction and makes final decisions after considering technical evidence and contributor feedback. This is a small community project; participation does not require employment, membership, or a contribution agreement.

## Roles and responsibilities

- **Project owner and lead maintainer — 4alvit.** Sets scope and priorities, resolves technical disputes, manages repository access and branch protection, and appoints additional maintainers with their agreement. Owns the maintenance of this document, the [roadmap](ROADMAP.md), and the accuracy of public project claims.
- **Release maintainer — 4alvit.** Reviews compatibility and upgrade notes, verifies the exact candidate and its evidence, approves stable promotion, and coordinates rollback or withdrawal when necessary. The [release process](RELEASING.md) defines the required steps. Publication is separate from an operator's decision to deploy to equipment.
- **Security contact — 4alvit.** Triages private reports, coordinates fixes and disclosure, credits reporters, and tracks the response commitments in [SECURITY.md](SECURITY.md). Repository access may allow other maintainers to assist, but access alone does not assign this responsibility.
- **Contributors and reviewers.** Propose focused changes, supply reproducible evidence, review behavior and failure paths, and follow [CONTRIBUTING.md](CONTRIBUTING.md) and the [code of conduct](CODE_OF_CONDUCT.md). Reviewers disclose important limitations, such as hardware that was not tested.
- **Automation.** The `californiantiramisu` account and GitHub Apps perform configured checks, reviews, and merge automation. Automation has only the permissions granted to it and does not constitute a second human maintainer, succession arrangement, or independent hardware acceptance.
- **Installation operators.** Control access to their own hosts, credentials, MQTT publishers, and equipment. They assess suitability, commission a candidate, and authorize physical deployment. The project cannot grant itself this authority.

The current repository review routing is recorded in [.github/CODEOWNERS](.github/CODEOWNERS). A name in that file is not a promise of availability and does not override protected-branch rules. No second human maintainer is currently documented by this project.

## Proposals and decisions

Use [GitHub Issues](https://github.com/victron-venus/inverter-control/issues) for bugs, feature requests, and substantial design proposals, and a pull request for the proposed implementation. Explain the affected users, alternatives, compatibility implications, risks, and validation. Changes to control behavior require explicit failure/recovery analysis; sensitive vulnerability details belong in the private reporting channel.

Maintainers seek agreement through public technical discussion. The owner records the accepted direction or a reason for declining or deferring a proposal in its issue or PR. Contributors may request reconsideration with new evidence; the owner has the final decision when consensus is not reached. Personal disagreements are handled under the code of conduct, not through changes to technical access controls.

Merges must satisfy the repository's current required checks and reviews. Automation may perform an authorized merge after those requirements are met; maintainers must not describe an automated approval as a human review. Changes to governance, maintainer appointments, release policy, and security commitments are proposed and reviewed in public PRs, except for confidential access details.

## Continuity and access

The public repository, MIT license, issue history, locked development environment, and release runbooks allow others to inspect and develop the code. They do **not**, by themselves, establish that another person can administer the existing repository or publish a release within one week.

A continuity arrangement must identify a willing, competent successor or backup maintainer, provide lawful recovery of the required repository, release, and identity access, and be exercised without disclosing credentials. Its non-secret record must state who is responsible, what actions are recoverable, and the date and outcome of the most recent exercise. Test issue creation/closure, a protected change, and candidate publication; coordinate any real publication through the normal release policy.

Until that arrangement has been confirmed and recorded, the project does not claim the OpenSSF `access_continuity` requirement or a human bus factor of two. Do not publish secrets, personal recovery instructions, or private contact details here. Review continuity when maintainers or signing/release credentials change and at least annually.

## Contribution rights

[CONTRIBUTING.md](CONTRIBUTING.md) requires contributors to have the right to submit their work under the MIT license and to preserve third-party notices. The project currently has no mandatory DCO sign-off or separate CLA. Existing commits may contain sign-offs, but that does not establish that all historical non-trivial contributors made a DCO assertion. Adoption of a stronger process and any retrospective review must be explicit; maintainers must not manufacture contributor signatures or assert rights on someone else's behalf.
