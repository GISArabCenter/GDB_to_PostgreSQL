def classFactory(iface):
    from .plugin import GDBPostgresPlugin
    return GDBPostgresPlugin(iface)
