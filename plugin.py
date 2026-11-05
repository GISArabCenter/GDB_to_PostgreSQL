import os
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction
from qgis.core import QgsApplication
from .provider import GDBPostgresProvider


class GDBPostgresPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.action = None

    def initGui(self):
        self.provider = GDBPostgresProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)
        self.action = QAction(QIcon(os.path.join(os.path.dirname(__file__), "icon.svg")),
                              "GDB to PostgreSQL", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("GDB to PostgreSQL", self.action)

    def run(self):
        import processing
        processing.execAlgorithmDialog("gdb_to_postgresql:gdb_to_postgres_domains_styles", {})

    def unload(self):
        if self.action is not None:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu("GDB to PostgreSQL", self.action)
            self.action.deleteLater()
            self.action = None
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
