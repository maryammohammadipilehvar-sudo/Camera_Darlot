# PRODUCT

## What this is

Autonomous AI video security for warehouses and multi-site facilities.
Cameras + Jetson edge compute + on-prem detection + configurable remote
notifications. The operator sees alerts on their phone — not on a wall
of monitors.

## Value proposition

On-prem Jetson-based detection with configurable remote notifications,
no cloud lock-in. Customers own the footage, own the hardware, and can
air-gap the site if they need to. No live-monitoring staff required —
the system tells someone when it matters.

## Target user

Facility operations teams at customer HQ, not per-site security guards.
A regional ops lead managing 4-20 warehouses, who needs to be notified
when someone is in a zone they shouldn't be in, or when a loud or
anomalous event fires at 3am. The dashboard is a periodic review
console, not a monitoring station.

## Competitive landscape

Verkada, Rhombus, and Avigilon own the market for cloud VMS plus smart
cameras. Our wedge is TBD, but the plausible differentiators are:

- **Self-hostable**: no forced cloud dependency
- **Air-gappable**: runs on an isolated LAN for customers with
  data-sovereignty or IT-security requirements
- **Lower per-camera cost**: one Jetson box handles multiple streams
  instead of one smart-camera-per-view
- **Customer-owned**: no subscription lock-in on the footage

Whether these are actually enough to win is an open GTM question.
Technically the stack can deliver all four.

## Explicit non-goals

- **Not a Verkada clone in one repo.** We won't ship a full VMS with
  enterprise access control, badge integration, and SSO as a
  prerequisite.
- **Not building a mobile app.** Notifications ride existing services
  (ntfy, Telegram, SMS) — we stay a detection + alerting product,
  not an app company.
- **Not cloud VMS.** No centralised multi-tenant video storage.
  Footage stays on-prem; the cloud touches nothing but notification
  routing if the customer opts in.

## Current state (2026-04-23)

- Detection + tracking + behavior + anomaly pipelines running on
  Jetson AGX Orin 64GB (stack in `CLAUDE.md`).
- MQTT alert publication wired into `security/alerts`.
- Dashboard web UI serves events, history, and a live stream proxy.
- **Critical gap:** no verified subscriber on the MQTT alert channel.
  Detection without notification means the system tells no one. See
  `FOLLOWUPS.md` top entry.
- Single-camera, single-site deployment only — multi-site architecture
  not yet designed (`FOLLOWUPS.md`).
