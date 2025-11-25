# Plugin Pretense
With this plugin, you can display persistent status update embeds for your Pretense missions.

## Configuration
As Pretense is an optional plugin, you need to activate it in main.yaml first like so:
```yaml
opt_plugins:
  - pretense
```
The configuration has to be done in config/plugins/pretense.yaml and is quite easy. You can define a channel per 
instance that you want your embeds to be displayed in. And you can define, how often they should be updated. Embeds will 
only be updated, if there is a pretense mission running and if there is new data to be displayed.

```yaml
DEFAULT:
  update_interval: 120        # interval in seconds when the embed should update (default = 120)
  DCS.dcs_serverrelease:
    json_file_path: '{instance.home}\Missions\Saves\player_stats.json'        # this is the default
  #json_file_path: '{instance.home}\Missions\Saves\player_stats_v2.0.json'  # use this for Pretense v2.0
  channel: 1122334455667788   # channel, where to upload the stats into (default: Status channel)
```
Every parameter has a default value, which means, that you do not need to specify a configuration file at all.

If you want to assign Discord roles based on the highest Pretense rank a user has on any server, add a `rank_roles`
section. The keys use the rank codes (E-1..E-9, O-1..O-10) and the values are role names or IDs.

```yaml
DEFAULT:
  rank_roles:
    E-1: 123456789012345678
    E-4: "Junior Pilot"
    O-2: 234567890123456789
```

## File Upload
You can upload a modified pretense*.json or player_stats.json by dragging and dropping them into your admin folder.

## File Download
If you want to download the pretense files, you can add this section to your admin.yaml:
```yaml
  - label: Pretense
    directory: '{server.instance.missions_dir}\Saves'
    pattern: '*.json'
```
This would work for Foothold and similar missions also.

## Credits
Credits to No15|KillerDog for implementing the base version of this plugin!
