# Warden and QuestLog Fluxer Feature and Dashboard Audit

Date: 2026-10-10

## Scope and architecture

- `/mnt/gamestoreage2/DiscordBots/wardenbot` is the Discord runtime and the feature reference.
- `/mnt/gamestoreage2/DiscordBots/questlogfluxer` is the Fluxer runtime.
- `/srv/ch-webserver` owns both web dashboards. Dashboard templates and APIs do not live in either bot repository.
- The dashboard migration in this review changes the shared web shell and navigation only. It does not claim that a dashboard form has a working Fluxer runtime consumer when none exists.

## Feature inventory

| Capability | Warden / Discord | QuestLog Fluxer runtime | Fluxer dashboard | Assessment |
| --- | --- | --- | --- | --- |
| Core status, help, invite | Yes | Yes | Server overview/settings | Present |
| XP, levels, rewards, leaderboards | Message, media, reaction, voice, gaming, invite, command, and LFG sources | Message/media and voice XP; reaction counts; game-event awards | Yes | Partial parity; gaming, invite, and reaction-award behavior differs, and Fluxer import/export controls still say coming soon |
| LFG creation, browse, calendar, attendance | Yes | Yes | Yes | Present; Fluxer cross-network broadcasting is not enabled in the runtime |
| Basic moderation | Yes | Ban, temp-ban, kick, timeout | Yes | Present at a basic level |
| Advanced moderation | Auto-mod, warnings, jail, mute, pardons | No equivalent runtime cog | Dashboard has warning/configuration surfaces | Missing runtime parity |
| Anti-raid and lockdown | Yes | No | No dedicated Fluxer controls | Missing |
| Member verification | CAPTCHA, account age, multi-step, quarantine | No verification cog | Configuration page exists | Dashboard/runtime mismatch |
| Audit events | Yes | Yes | Yes | Present, but Fluxer moderation still has an audit integration TODO |
| Role management/templates | Yes | Dashboard action consumer in core | Yes | Present through queued dashboard actions |
| Advanced role workflows | Temporary roles, member requests/approvals, export, permission diff, moderator audit, emergency strip | No equivalent command set | Bulk assignment, creation, import, and templates | Missing runtime parity |
| Reaction roles | Yes | Yes | Yes | Present |
| Welcome and goodbye messages | Yes | Yes | Yes | Present |
| Message/embed tools | Yes | Embed action consumer | Yes | Direct embeds present; broadcast and scheduling are marked bot pending |
| Scheduled messages | Yes | No scheduled-message processor | Disabled controls | Missing |
| Channel administration | Templates, bulk slowmode/delete, archive/clone, access audit, locks, notifications | Template actions only | Templates and role/channel imports | Missing most runtime controls |
| Suggestions and feedback | Submission, staff review, listing, and setup | No equivalent | No | Missing |
| RSS delivery | Yes | Yes | Discovery/RSS article surfaces | Present |
| Creator and game discovery | Yes | Yes | Yes | Present |
| Featured creators and nominations | Yes | Yes | Featured creators/spotlight | Present |
| Twitch/YouTube live alerts | Yes | Yes | Yes | Present |
| Flair synchronization/store | Yes | Yes | Yes | Present |
| Chat bridge | Optional, guarded | Yes | Yes | Present |
| Member/channel synchronization | Yes | Yes | Settings/status surfaces | Present |
| Channel directory | Yes | No dedicated cog | No | Missing |
| QuestLog Network LFG delivery | Yes | Broadcast code is commented out | Network configuration exists | Missing runtime delivery |
| Game server controls | Yes | Yes, plus 7DTD, V Rising Quest, and Soulmask modules | Managed through Quest Control, not the Fluxer guild dashboard | Present outside this dashboard |
| FFXIV timers | Yes | Yes | No guild-dashboard configuration | Present in runtime, dashboard gap |
| Site activity tracking | Optional legacy path | Yes | No dedicated surface | Present in runtime, dashboard gap |
| Emergency host controls | Optional, explicitly gated | No | No | Intentionally not ported because it is Discord-owner and host specific |

## Dashboard design findings

Warden had already moved to the current QuestLog dashboard system:

- `base_web.html` supplies the shared QuestLog header, density, navigation, and responsive behavior.
- A platform-owned shell reserves a fixed 16 rem management rail and full-width content area.
- Server navigation is grouped into Community, Engagement and rewards, Safety and access, Content and automation, and Server.
- Native `details` groups replace several custom local-storage submenu handlers.
- Mobile navigation uses one overlay and one accessible toggle.

Fluxer still used legacy dashboard and member bases that duplicated the site header, mixed member and administration links, repeated several destinations, and maintained custom collapse state. The port replaces both legacy bases and their sidebar markup with the same QuestLog shell and grouped navigation pattern while retaining Fluxer's purple platform accent and all existing destinations. Featured Creators, Leaderboards, and Found Games are shared between public member and management URLs, so those views now select the correct new shell explicitly instead of hard-wiring one side's layout.

## Missing-feature priorities

1. Make the verification page truthful by implementing a Fluxer verification runtime or marking the page unavailable until one exists.
2. Close the advanced-moderation gap: warnings, pardons, auto-moderation, and an explicit audit write from every moderation action.
3. Decide which advanced role and channel workflows are safe and supported by Fluxer's API, then expose only those verified operations.
4. Add the scheduled-message processor and enable the dashboard's existing schedule/broadcast controls only after end-to-end delivery tests pass.
5. Implement canonical QuestLog Network LFG delivery for Fluxer; the current runtime explicitly leaves broadcasting disabled.
6. Add a channel-directory equivalent if Fluxer exposes the required channel and permission APIs.
7. Decide whether suggestions/feedback should be cross-platform or remain Discord-only.
8. Add dashboard configuration/status for FFXIV timers and activity tracking only if these are intended as per-guild supported products.

## Migration boundary

The migration intentionally does not add security or feature behavior. The next pass should security-review the Fluxer runtime and the web dashboard/API boundary together, especially the pending action queue, guild authorization, cross-platform bridge, remote media, and game-server control paths.
