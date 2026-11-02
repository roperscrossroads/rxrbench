# Security

rxrbench writes to hardware. A bug that lets it write to the wrong device, bypass a refusal
(`devreg guard`, the RF rules, the apply gate) or leak a key or a site's identifiers is a
security issue.

Report one privately, through GitHub's private vulnerability reporting (the repository's
Security tab, "Report a vulnerability"), rather than in a public issue, with the layout and the command that reached it; a redacted `runs/<time>/` record helps.
Expect an acknowledgement within a week.

What the tool promises, and where it is enforced, is in `docs/ARCHITECTURE.md` "Safety rules, in
code". A site's own secrets never belong in this repository (`CONTRIBUTING.md#public-and-private`).
