# Third-party notices

## OliveTin

This project is **based on and extends an OliveTin deployment**, and is not an official OliveTin release. It adds a Python loopback API, a custom frontend, registration and lifecycle adapters, preferences, icons and a recycle bin. The new UI calls the Python adapters; the original OliveTin command console is retained as a secondary interface. OliveTin core source is not vendored or modified here.

- Upstream: https://github.com/OliveTin/OliveTin
- Locally tested upstream release: 3000.20.0
- Upstream license: GNU Affero General Public License, version 3
- Preserved license text: `LICENSES/OliveTin-AGPL-3.0.txt`
- The OliveTin brand image at `ui/assets/logo.png` is retained from the local upstream deployment. Names and logos remain associated with their respective owners.
- The OliveTin executable and web UI release package are intentionally not bundled. Obtain them from the upstream project together with its notices and source information.

## Tabler Icons

The SVG icons in `ui/assets/icons/` are from Tabler Icons (the local implementation used version 3.49.0).

- Upstream: https://github.com/tabler/tabler-icons
- Copyright (c) 2020–2026 Paweł Kuna
- License: MIT
- Preserved text: `LICENSES/Tabler-MIT.txt` and `ui/assets/icons/LICENSE`

## Publication assets

The public source snapshot uses a CSS gradient instead of the local installation's private wallpaper. It does not include uploaded icons, actual service registries, application archives, model data, logs, local backups, credentials, screenshots or host-specific launch-agent files. Application icons in a user's installation remain local user-supplied resources.

The custom additions do not currently declare a separate open-source license. These notices preserve the upstream licenses; they do not replace them or claim ownership of upstream work.
