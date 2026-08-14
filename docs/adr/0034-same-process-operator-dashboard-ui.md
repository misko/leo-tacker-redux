# ADR 0034: Same-process operator dashboard UI

- Status: accepted
- Date: 2026-08-13

## Decision

Dashboard v1 serves an accessible HTML/CSS/vanilla-JavaScript operator shell
from the existing loopback dashboard process. A composite application owns a
fixed allow-list for `/`, `/assets/dashboard.css`, and
`/assets/dashboard.js`, then delegates all other requests to the unchanged JSON
application. Static content is packaged with the Python distribution and read
once when the dashboard application is assembled. No separate frontend
runtime, Node project, cloud host, or fourth product service is introduced.

The shell consumes only the existing same-origin read API. It displays the
selected UTC half-open interval, scan/dwell counts by radio, recent recordings,
recording detail and independent features, exact-ID detector evaluations,
exact-ID/release-alias models, tracks, and storage health. It has explicit
loading, empty, error, browser-stale, and missing-object states. API response
schemas do not change.

Static route matching is exact. Unknown asset paths receive a generic response
without path echo. HTML and JSON are `no-store`; the two non-fingerprinted
assets have a five-minute cache bound. The transport retains `nosniff`, and
static responses add a same-origin content security policy, frame denial, and
no-referrer policy. The listener remains loopback-only. Remote access requires
the site's existing authenticated TLS reverse proxy or an operator SSH tunnel.

## Honest availability boundaries

The v1 API has no enumeration query for models or evaluations, so those panels
require an immutable ID or approved release alias. It also has no typed LNB
parameter, covariance, satellite-association, or track-covariance view. The UI
labels those gaps and does not inspect canonical reports, expose locators,
infer values, or persist plots. “Stale” describes time since this browser's
last successful refresh because projection-age metadata is not yet available.

## Consequences

Operators receive a useful local interface while the dashboard remains a
read-only projection consumer. Assets and API share an origin, avoiding CORS
and another deployment boundary. A future typed API may add missing scientific
detail without replacing the shell or opening raw object access. Asset updates
may take up to five minutes in an already-open browser.

## Verification

Tests cover the fixed route allow-list, traversal rejection, content types,
cache and security headers, unchanged API delegation, same-listener transport,
deployment composition, landmarks/labels/focus hooks, responsive/reduced-motion
CSS, state hooks, half-open query construction, and component import rules.
The wheel build must contain all three packaged static files.
