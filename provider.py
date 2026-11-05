from qgis.core import QgsProcessingProvider
from .algorithm import GDBToPostgresWithDomainsAndStyles


class GDBPostgresProvider(QgsProcessingProvider):
    def id(self):
        return "gdb_to_postgresql"

    def name(self):
        return "GDB to PostgreSQL"

    def longName(self):
        return self.name()

    def loadAlgorithms(self):
        self.addAlgorithm(GDBToPostgresWithDomainsAndStyles())
