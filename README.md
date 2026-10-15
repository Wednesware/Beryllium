[![Wednesware](wednesware.png)](https://wednesware.org)

# Beryllium

A Python LAN hosting framework for interactive Fluorine pages, multiplayer sessions, and real-time client management.

Beryllium uses FastAPI and WebSockets to host one or more Fluorine pages. Each browser tab has its own `Client`, clients are organized into IP-based `Group` objects, and pages can be presented to individual clients, groups, or everyone using a persistent browser connection.

## Installation

> `n2 get beryllium`

If you don't have Nitrogen: `pipx install wwn` first.

Install the runtime dependencies listed below as needed for your environment.

## Quick start

### Basic LAN host

```python
from nitrogen import require

Host = require("be").Host
Page = require("f").Page
h1 = require("f.structuring").h1

host = Host(port=8000)
page = Page("home")
page.body(h1("Hello from Beryllium!"))
host.page = page
host.run(open=True, admin=False)
```

The server listens on your LAN by default. `open=True` opens the served page in a browser; `admin=False` prevents automatic launching of the administration panel.

### Present pages to individual clients

```python
from nitrogen import require

Host = require("be").Host
Page = require("f").Page
h1 = require("f.structuring").h1
button = require("f.structuring").button

host = Host(port=8000)
lobby = Page("lobby")
game = Page("game")
host.page = lobby


def play(client):
    client.present(game)


def leave(client):
    client.present(None)


lobby.body(
    h1("Lobby"),
    button("Play", onclick=play)
)
game.body(
    h1("Game"),
    button("Back to lobby", onclick=leave)
)

host.expose("Lobby", lobby)
host.expose("Game", game)
host.run(open=True)
```

Callbacks declared in Fluorine button attributes are routed to Python. If a callback accepts a parameter named `client`, Beryllium injects the initiating `Client`. `client.present(None)` removes the client's page override.

### Present pages to groups

```python
@host.on_connect
def connected(client):
    print(client.name, "connected")
    # Show one page to every session belonging to the client's group.
    client.group.present(game)
```

Group presentations do not replace an individual client's explicit scene override. Changing `host.page` sets the default for clients with neither a client nor a group override.

## Dependencies

- Python 3.12+
- Nitrogen (`wwn`)
- Fluorine
- Magnesium (colors used for console output)
- FastAPI
- Uvicorn
- Sulfur (optional; used for the desktop administration window)

# Definitions

## `beryllium`

From the base library, import the host, client, and group APIs.

> `Host = require("be").Host`

### `beryllium:Host(port: int = 8000, host: str = "0.0.0.0", max_clients: int | None = None, bans_file: str = "beryllium_bans.json", admin_panel: bool = True)`

Creates a LAN host and manages HTTP routes, WebSocket sessions, scene presentation, events, and administration.

> `host = Host(port=8000, max_clients=16)`

#### `beryllium:Host.page`

The default Fluorine page, inherited by clients that do not have a client or group override.

> `host.page = lobby`

#### `beryllium:Host.run(open: bool = False, debug: bool = False, static_dir: str | None = None, admin: bool | None = None, sulfur: bool = True)`

Starts the host. `debug=True` enables verbose server logs; `static_dir` mounts static files at `/static`. When administration is enabled, Sulfur may display the host panel in a desktop window; otherwise the panel can be opened in a local browser.

> `host.run(open=True, admin=True)`

#### `beryllium:Host.expose(name_or_func, func=None)`

Exposes an object to the administration panel's Python console. When the object is a compatible Fluorine page, it is also added as a named scene to the admin scene selectors and `present` command.

> `host.expose("Lobby", lobby)`

#### `beryllium:Host.refresh_page(page: Page | None = None)`

Rebuilds a page and updates clients currently viewing it. With no argument, refreshes the default page.

> `host.refresh_page(game)`

#### `beryllium:Host.on_join(fn)`

Registers a callback invoked when a new session joins via an HTTP page request.

> `@host.on_join` followed by `def joined(client): ...`

#### `beryllium:Host.on_connect(fn)`

Registers a callback invoked when a client establishes its first active WebSocket connection.

> `@host.on_connect` followed by `def connected(client): ...`

#### `beryllium:Host.on_disconnect(fn)`

Registers a callback invoked after a client's last WebSocket connection closes.

> `@host.on_disconnect` followed by `def disconnected(client): ...`

#### `beryllium:Host.action(key: str)`

Registers a Python callback for an HTTP action available at `/api/<key>`. A `client` parameter is injected when declared by the callback.

> `@host.action("start")`

#### `beryllium:Host.on(key: str)`

Registers a handler for named WebSocket events emitted by browser clients.

> `@host.on("ready")`

#### `beryllium:Host.notify(message: str, title: str = "Notice", level: str = "info", duration: int = 10000, client=None)`

Displays a notification to all clients or a specific client/group. Supported levels: `info`, `warning`, `error`, `success`.

> `host.notify("Game starting!", title="Announcement")`

#### `beryllium:Host.broadcast(name: str, data=None)`

Broadcasts a visible notification when `data` is omitted, or a named JavaScript event when it is supplied.

> `host.broadcast("round_started", {"round": 1})`

#### `beryllium:Host.send(client_id, name: str, data=None)`

Sends a named event to a `Client`, `"HOST"`, or a `(group_id, client_id)` tuple.

> `host.send(client, "score", {"value": 10})`

#### `beryllium:Host.update(selector: str, *, text=None, html=None, value=None, styles=None, attributes=None, client=None)`

Updates matching DOM elements for all clients or a particular client/group.

> `host.update("#status", text="Ready", client=client)`

#### `beryllium:Host.notification_style(css: str)`

Adds notification styling before the server is started.

> `host.notification_style(".brlm-notification{border-radius:16px}")`

#### `beryllium:Host.apply_styling()`

Applies Beryllium's default styling preset to `host.page`.

> `host.apply_styling()`

### `beryllium:Client`

Represents a browser session (typically a browser tab). Clients belong to a `Group` and expose `id`, `full_id`, `name`, `group`, `status`, `device_type`, and other session information.

#### `beryllium:Client.present(page: Page | None)`

Presents a Fluorine page to this client. `None` restores inheritance from its group or the host default.

> `client.present(game)`

#### `beryllium:Client.rename(name: str)`

Changes the name displayed in the admin panel.

> `client.rename("Player 1")`

#### `beryllium:Client.pause(reason: str = "Session paused")`

Pauses the client's ability to interact with the host.

> `client.pause("Round ended")`

#### `beryllium:Client.resume()`

Resumes a paused session.

> `client.resume()`

#### `beryllium:Client.kick(reason: str = "Removed by host")`

Removes the client from active participation.

> `client.kick("Disconnected by host")`

#### `beryllium:Client.send(name: str, data=None)`

Sends a WebSocket event directly to the client.

> `client.send("turn", {"player": 1})`

#### `beryllium:Client.update(selector: str, **kwargs)`

Updates elements in this client's currently displayed page.

> `client.update("#status", text="Your turn")`

#### `beryllium:Client.notify(message: str, title: str = "Notice", level: str = "info", duration: int = 10000)`

Displays a notification for this client only.

> `client.notify("Welcome!")`

#### `beryllium:Client.online()` / `beryllium:Client.offline()`

Reports whether the client has an active WebSocket connection.

> `if client.online(): print("Connected")`

### `beryllium:Group`

Represents the clients sharing a peer IP address. A group is iterable and supports indexed access to its clients.

> `for client in group: print(client.name)`

#### `beryllium:Group.present(page: Page | None)`

Changes the scene inherited by group members without client-level overrides. `None` restores the host default.

> `client.group.present(game)`

#### `beryllium:Group.rename(name: str)`

Renames the group in the administration panel.

> `client.group.rename("Team A")`

#### `beryllium:Group.pause(reason: str = "Group paused")` / `beryllium:Group.resume()`

Pauses or resumes the group.

> `client.group.pause("Intermission")`

#### `beryllium:Group.kick(reason: str = "Group kicked")` / `beryllium:Group.ban(reason: str = "Group banned")`

Disconnects or bans a group; bans persist through the configured bans file.

> `client.group.ban("Access revoked")`

#### `beryllium:Group.notify(message: str, title: str = "Notice", level: str = "info", duration: int = 10000)`

Displays a notification for all members of a group.

> `client.group.notify("Round begins!")`

### `beryllium:ClientGroups`

The host's client/group registry, available as `host.cgroups`. It supports group indexing and iteration.

> `group = host.cgroups[0]`

#### `beryllium:ClientGroups.get_host()`

Returns the internal host client.

> `host_client = host.cgroups.get_host()`

#### `beryllium:ClientGroups.by_ip(ip: str)`

Looks up a group by its peer IP.

> `group = host.cgroups.by_ip("192.168.1.5")`

#### `beryllium:ClientGroups.unban(ip: str)`

Removes an IP ban and restores a banned group's active status if present.

> `host.cgroups.unban("192.168.1.5")`

## Browser API

Beryllium adds a JavaScript client runtime to every hosted browser page.

### `window.beryllium`

Exposes `clientId`, `groupId`, `connected`, `on(name, callback)`, `emit(name, data)`, and `notify(message, title, level, duration)`.

> `beryllium.emit("ready", {player: "Alice"})`

### `berylliumAction(key, data = {})`

Calls a registered Python action using an HTTP POST request.

> `berylliumAction("start")`

# Administration

The local-only host panel provides connected device details, scene management, notifications, command consoles, and client/group controls. The Python console executes arbitrary Python in the host process, so keep administration restricted to trusted local access.

## Scenes

Expose Fluorine pages to add them to the **Scenes** section. The section is hidden if no scenes are exposed. Scene selectors present a page immediately when changed, globally or for individual groups/clients. Each client row shows the effective scene.

Scene resolution follows this order:

> `Client.present(...)` → `Group.present(...)` → `Host.page`

Selecting **Inherit** on a client or group clears its explicit override.

## Command console

The admin command console supports these commands:

| Command | Purpose |
| --- | --- |
| `help` | Show supported commands |
| `groups` | List groups |
| `clients [G]` | List clients, optionally within a group |
| `present global SCENE` | Change the default scene |
| `present G SCENE` | Present a scene to a group |
| `present G:C SCENE` | Present a scene to a client |
| `present G[:C] inherit` | Clear a client/group override |
| `pause G[:C] [reason]` | Pause a client or group |
| `resume G[:C]` | Resume a client or group |
| `kick G[:C] [reason]` | Kick a client or group |
| `ban G [reason]` | Ban a group |
| `rename G[:C] NAME` | Rename a client or group |
| `unban IP` | Remove an IP ban |
| `notify G[:C] MESSAGE` | Notify a client or group |
| `broadcast MESSAGE` | Notify all clients |

Scene names passed to `present` must have been exposed through `host.expose(...)`. Names containing spaces are supported.

## Notes

- Presentation swaps page markup over WebSockets without navigating the browser away from Beryllium's persistent shell.
- Group membership is based on peer IP, not an application-defined room or team identifier.
- Browser action handlers are restricted to the client's effective page.
- The host panel is local-only, but the hosted application is reachable over the configured network interface.
- Assign scene pages before starting the host; use `refresh_page()` after changing content of an existing page.
