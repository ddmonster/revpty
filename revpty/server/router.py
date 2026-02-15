class SessionRouter:
    def __init__(self):
        self.clients = {}
        self.browsers = {}

    def register(self, session, role, ws):
        if role == "client":
            self.clients[session] = ws
        else:
            self.browsers[session] = ws

    def peer(self, session, role):
        return (
            self.browsers.get(session)
            if role == "client"
            else self.clients.get(session)
        )

    def unregister(self, ws):
        for d in (self.clients, self.browsers):
            for k, v in list(d.items()):
                if v is ws:
                    del d[k]
