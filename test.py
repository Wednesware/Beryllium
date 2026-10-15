
from nitrogen import require
from beryllium import Host, apply_styling

Page = require("f").Page
h1 = require("f.structuring").h1
p = require("f.structuring").p
button = require("f.structuring").button

host = Host(port=8000)

# Pages
lobby = host.expose(apply_styling(Page("lobby")))
game = host.expose(apply_styling(Page("game")))

# Default page for all clients
host.page = lobby

# Python callbacks receive the client that clicked.
def play(client):
    print(f"{client.name} entered the game!")
    client.present(game)

def leave(client):
    print(f"{client.name} returned to the lobby!")
    client.present(None)

# Lobby
lobby.body(
    h1("Welcome to the lobby!"),
    p("Press the button to start playing."),
    button("Play", onclick=play)
)

# Game
game.body(
    h1("You're in the game!"),
    p("This page is only visible to you."),
    button("Back to lobby", onclick=leave)
)

@host.on_connect
def connected(client):
    print(f"{client.name} connected!")

# Start Beryllium
host.run(open=True, admin=True, debug=False)