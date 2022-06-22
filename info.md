{% if prerelease %}

This is a pre-release version
It may contain bugs or break functionality in addition to adding new features and fixes. Please review open issues and submit new issues to the [GitHub issue tracker](https://github.com/bretterer/home-assistant-rivian/issues).

{% endif %}

# Home Assistant integration for Rivian (Unofficial)
[![GitHub Release][releases-shield]][releases]
![GitHub all releases][download-all]
![GitHub release (latest by SemVer)][download-latest]
[![GitHub Activity][commits-shield]][commits]


This integration is an unofficial Rivian integration for Home Assistant, installed through [HACS](https://hacs.xyz/docs/setup/download).

In order to use this extension, you will need the following information
 - Rivian Username
 - Rivian Password
 - Client ID
 - Client Secret
 - VIN

Your vehicle must be in delivered status and in your posession for this integration to function. The client id and client secret must be obtained outside of this integration and there is currently no official or unofficial way to do this. You can search forums or the [Rivian Discord][rivian-discord]

![Home Assistant Rivian Card](.github/images/home_assistant_rivian_entity_card.png)

{% if not installed %}

## Installation

1. Click install.
2. Reboot Home Assistant.
3. Hard refresh browser cache.
4. [![Add Integration][add-integration-badge]][add-integration] or in the HA UI go to "Configuration" -> "Integrations" click "+" and search for "Rivian (Unofficial) Integration".

{% endif %}


[commits-shield]: https://img.shields.io/github/commit-activity/w/bretterer/home-assistant-rivian?style=flat-square
[commits]: https://github.com/bretterer/home-assistant-rivian/commits/main
[releases-shield]: https://img.shields.io/github/release/bretterer/home-assistant-rivian.svg?style=flat-square
[releases]: https://github.com/bretterer/home-assistant-rivian/releases
[download-all]: https://img.shields.io/github/downloads/bretterer/home-assistant-rivian/total?style=flat-square
[download-latest]: https://img.shields.io/github/downloads/bretterer/home-assistant-rivian/latest/total?style=flat-square
[add-integration]: https://my.home-assistant.io/redirect/config_flow_start?domain=rivian
[add-integration-badge]: https://my.home-assistant.io/badges/config_flow_start.svg
[rivian-discord]: https://discord.gg/jEc5RUPd