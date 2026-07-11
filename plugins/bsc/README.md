# Plugin BSC
With this plugin, you can display persistent status update embeds for your BSC missions
(running on the DCS Pretense framework) and aggregate per-pilot XP/rank into a global
leaderboard.

## Configuration
As BSC is an optional plugin, you need to activate it in main.yaml first like so:
```yaml
opt_plugins:
  - bsc
```
The configuration is done in `config/plugins/bsc.yaml`. You can define a channel per
instance that you want your embeds to be displayed in, and how often they should be updated.
Embeds will only be updated if there is a mission running.

```yaml
DEFAULT:
  update_interval: 120        # interval in seconds when the embed should update (default = 120)
  stats_dir: 'D:\PlayerStats' # global directory of per-UCID JSON stat files (this is the default)
  DCS.dcs_serverrelease:
    # optional override; the plugin auto-detects the latest pretense_<theatre>_2.0.lua
    # in the server's Missions\Saves directory if this is not set.
    #zone_file_path: '{instance.home}\Missions\Saves\pretense_caucasus_2.0.lua'
    channel: 1122334455667788   # channel where to upload the stats into (default: Status channel)
```

### Data sources
The mission script writes per-player stats into a single global directory (default
`D:\PlayerStats`), with one JSON file per UCID (`<ucid>.json`). The leaderboard is built
by aggregating those files. Zone/front-line data is read from the per-server
`pretense_<theatre>_2.0.lua` persistence file under `{instance.home}\Missions\Saves` (the
filename is produced by DCS and must not be renamed).

Every parameter has a default value, so a configuration file is only required if you want to
change a channel, change the stats directory, or set up rank roles.

### Automatic restart on low FPS
If a server's FPS stays below a minimum for a configurable period, the plugin can restart it
automatically. Add an `fps_restart` section (per instance or in DEFAULT) to enable it:

```yaml
DEFAULT:
  fps_restart:
    min: 10           # restart when the FPS is below this value (default: 10)
    period: 5         # ... for at least this many minutes (default: 5)
    shutdown: false   # true: shut down and restart the whole DCS server, false: restart the mission only (default: false)
    populated: true   # false: do not restart while players are online (default: true)
    mentioning: true  # mention the admins in the alert message (default: true)
    message: Server is being restarted due to low performance.  # popup shown to players before the restart
```

All keys are optional - `fps_restart: {}` enables the check with the defaults shown above.
Servers in maintenance mode and servers with a restart already pending are never touched.
The FPS readings come from the monitoring plugin's `perfmon` event, so the (default) monitoring
plugin has to be loaded.

Because `perfmon` only fires once per 3600 sim frames, a server that is nearly frozen stops
reporting at all. A watchdog therefore also restarts any running, unpaused server that has not
sent a single FPS reading for `2 * 3600 / min` seconds (12 minutes at the default `min: 10`) -
silence that long means the sim is far below the minimum or hung. A reading barely above `min`
(less than 20% above) neither starts nor clears a low-FPS streak, so a single lucky sample
cannot reset the period.

### Rank roles
If you want to assign Discord roles based on the highest BSC rank a user has on any
server, add a `rank_roles` section. The keys use the rank codes (E-1..E-9, O-1..O-10) and the
values are role names or IDs.

```yaml
DEFAULT:
  rank_roles:
    E-1: 123456789012345678
    E-4: "Junior Pilot"
    O-2: 234567890123456789
```

### Sleekplan feedback integration
The plugin announces new [Sleekplan](https://sleekplan.com) feedback activity as embeds in a Discord
channel. It uses **Sleekplan webhooks** (real-time, push-based) - Sleekplan POSTs an event to the
bot the moment something happens, so there is no polling.

> **Why webhooks and not the REST API?** Sleekplan's REST *list* endpoint (`GET /v1/post`) currently
> returns HTTP 500 server-side, so polling for new posts is not possible. Webhooks deliver the full
> object on each event and are the supported real-time mechanism.

**Events announced:**
- `post` → 🆕 new feedback (`item.create`)
- `status` → 🔄 status changes such as *Planned → In Progress* (`item.update`, only when the status
  actually changed - other edits are ignored)
- `comment` → 💬 new comments (`comment.create`)

#### 1. Enable the bot's web service
The webhook is received by the bot's FastAPI web service. Create `config/services/webservice.yaml`:
```yaml
DEFAULT:
  debug: false
  listen: 0.0.0.0      # or 127.0.0.1 if you reverse-proxy on the same host
  port: 9876
```

#### 2. Expose it over HTTPS
Sleekplan only calls **HTTPS** endpoints, so put a reverse proxy in front of port `9876` with a
public hostname, e.g. `https://feedback.bscservers.ch` → `http://127.0.0.1:9876`.

#### 3. Configure the plugin
```yaml
DEFAULT:
  sleekplan:
    channel: 1218984048035106936            # Discord channel for announcements
    product_id: 647288130                   # your Sleekplan product id (ignore events from others)
    board_url: https://bsc.sleekplan.app    # used to build clickable post links
    events: [post, comment, status]         # which events to announce
    webhook:
      secret: 'A_LONG_RANDOM_SECRET'        # shared secret; Sleekplan must send it as ?key=
      path: /sleekplan                      # route path (default /sleekplan)
    # debug: true                           # log each received webhook action+data (INFO level)
```

#### 4. Register the webhook in Sleekplan
In Sleekplan go to **Settings → Webhooks** and add the endpoint:
```
https://feedback.bscservers.ch/sleekplan?key=A_LONG_RANDOM_SECRET
```
Enable the events `item.create`, `item.update` and `comment.create`. Requests whose `?key=` does not
match `webhook.secret` are rejected with 403; events for other `product_id`s are ignored.

Status-change detection keeps the last-known status per post in `config/plugins/bsc_sleekplan.json`.

## Commands
- `/bsc stats [user]` - show BSC stats (XP, rank) for yourself or for the given user, read directly from their `<ucid>.json` file.
- `/bsc leaderboard` - show the global top-50 pilots by XP, with medal emojis for the podium.
- `/bsc reset <what> [server]` - reset progress (see below).

## File Upload
You can upload a modified `pretense*.json` or `pretense*.lua` save file by dragging and dropping
it into your admin folder. Per-UCID player stat JSON files are not uploaded via Discord - they
live in the global `stats_dir`.

## File Download
If you want to download the save files, you can add this section to your admin.yaml:
```yaml
  - label: BSC
    directory: '{server.instance.missions_dir}\Saves'
    pattern: 'pretense_*.{json,lua}'
```

## Reset
`/bsc reset` accepts:
- `persistence` - deletes `pretense_*.lua` / `pretense_*.json` for the chosen server (server required, must be stopped)
- `statistics` - clears the **global** `stats_dir` - this affects every server
- `roles` - removes all configured rank roles from members
- `all` - all of the above for the chosen server

## Credits
Credits to No15|KillerDog for implementing the base version of this plugin.
