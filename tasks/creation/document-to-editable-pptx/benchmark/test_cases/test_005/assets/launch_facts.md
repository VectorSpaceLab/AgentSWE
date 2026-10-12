# Aster Field Launch Facts

## Product and Audience

Aster Field is a fictional mobile application for trained distributor technicians who inspect installed industrial pumps. Version 1.0 launches on 15 September 2026 for the three cohorts in `rollout.csv`.

## Supported Workflow

1. A trainer assigns a prepared inspection package to a licensed technician while the device is online.
2. The technician can open the package, record checklist responses, add text notes, and attach up to five photos while offline.
3. The technician marks the package ready to sync.
4. When connectivity returns, the technician starts synchronization and waits for server confirmation.
5. A supervisor reviews the submitted package in the existing distributor portal.

## Boundaries

- Version 1.0 does not create new inspection templates.
- It does not diagnose equipment, calculate a maintenance interval, approve a repair, or replace the distributor portal.
- A device-local **Ready to sync** state is not proof that the server received the inspection.
- Photos remain optional. The workflow must still be teachable without screenshots or photographic examples.
- A package with a persistent sync error must be escalated through the distributor's existing support channel; this training does not define that channel.

## Training Scenarios

### Scenario A

The technician completes a package underground with no signal. The correct teaching point is to mark it ready to sync, retain the device, and synchronize only when connectivity returns.

### Scenario B

The device says **Ready to sync**, but the supervisor cannot see the package. The correct teaching point is that server confirmation has not occurred. The technician must start sync when online and verify the **Synced** state.

## Source Boundary

All product claims and user-facing wording must come from this file and `approved_copy.csv`. Do not invent security, encryption, retention, device, integration, performance, or service-level claims.
