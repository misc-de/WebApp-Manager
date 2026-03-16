from gi.repository import GObject


class Entry(GObject.GObject):
    id = GObject.Property(type=int)
    title = GObject.Property(type=str, default='')
    description = GObject.Property(type=str, default='')
    active = GObject.Property(type=bool, default=True)

    def __init__(self, id, title, description='', active=True):
        super().__init__()
        self.id = id
        self.title = title
        self.description = description
        self.active = active
