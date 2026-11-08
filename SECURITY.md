# Security

Email Archive Explorer is a **local, single-user** tool: it serves a web UI on
your own machine so you can read your own mail archives. The design goal is
that neither the archive contents nor the machine can be reached by anything
other than your own browser tab.

## Threat model

| Concern | Protection |
|---|---|
| Archive integrity | Archives are opened read-only; no code path writes to them. Indexes live separately in `.cache/`. |
| Network exposure | Binds to `127.0.0.1` by default. A non-loopback `--host` prints a warning and disables the file browser unless `--allow-remote-browse` is given. |
| Other websites reading your mail | No CORS headers are sent, so pages on other origins can't read API responses. |
| DNS rebinding | Requests whose `Host` header isn't `127.0.0.1`, `localhost` or `::1` (or the `--host` you chose) get `421`. |
| Cross-site request forgery | State-changing requests and file-system routes need a per-process session token, injected into the page and sent as `X-EAE-Token`. |
| File-system browsing | Confined to Home, `data/`, mounted drives and `--browse-root` folders; `..` and symlinks are resolved before the containment check. |
| Malicious HTML mail | Rendered in a sandboxed `<iframe>` (no scripts, no same-origin access) with a CSP that blocks network loads until you allow remote images. |
| Malicious attachments | Inline HTML/SVG responses carry `Content-Security-Policy: sandbox`; every attachment response sends `X-Content-Type-Options: nosniff`. ZIP/TAR attachments are listed in memory, never extracted to disk. |

Out of scope: multi-user deployments and exposure to untrusted networks. If you
bind to a non-loopback address, anyone who can reach the port can read the open
archive.

## Reporting a vulnerability

Please report security issues privately through GitHub's **Report a
vulnerability** (Security → Advisories) on this repository rather than in a
public issue. Include steps to reproduce and the version or commit you tested.
You should get a response within a week.
