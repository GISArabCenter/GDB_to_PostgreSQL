"""Restore PostgreSQL layer QML from public.layer_styles on project layer insertion."""
import os
import tempfile
from qgis.core import QgsDataSourceUri, QgsProject, QgsProviderRegistry


class PostgresStyleAutoLoader:
    def __init__(self):
        self._busy = set()

    def start(self):
        QgsProject.instance().layerWasAdded.connect(self.apply)
        for layer in QgsProject.instance().mapLayers().values():
            self.apply(layer)

    def stop(self):
        try:
            QgsProject.instance().layerWasAdded.disconnect(self.apply)
        except (TypeError, RuntimeError):
            pass

    def apply(self, layer):
        if not layer or not layer.isValid() or layer.providerType() != 'postgres':
            return
        lid = layer.id()
        if lid in self._busy:
            return
        self._busy.add(lid)
        try:
            # QGIS-native style lookup takes priority whenever it works.
            related, *_ = layer.listStylesInDatabase()
            if related:
                return
            uri = QgsDataSourceUri(layer.source())
            schema, table, geom = uri.schema(), uri.table(), uri.geometryColumn()
            if not (schema and table):
                return
            metadata = QgsProviderRegistry.instance().providerMetadata('postgres')
            connection = metadata.createConnection(layer.source(), {})
            # Never interpolate identifiers or user input into SQL.
            def literal(s):
                return "'" + str(s).replace("'", "''") + "'"
            sql = (
                "SELECT styleqml::text FROM public.layer_styles "
                "WHERE f_table_catalog = current_database() "
                "AND f_table_schema = {schema} AND f_table_name = {table} "
                "AND (f_geometry_column = {geom} OR f_geometry_column IS NULL) "
                "AND useasdefault IS TRUE "
                "ORDER BY CASE WHEN f_geometry_column = {geom} THEN 0 ELSE 1 END, id DESC LIMIT 1"
            ).format(schema=literal(schema), table=literal(table), geom=literal(geom))
            rows = connection.executeSql(sql)
            if not rows or not rows[0] or not rows[0][0]:
                return
            fd, path = tempfile.mkstemp(suffix='.qml', prefix='gdb_pg_style_')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as out:
                    out.write(str(rows[0][0]))
                result = layer.loadNamedStyle(path)
                if isinstance(result, tuple) and len(result) > 1 and result[1]:
                    layer.triggerRepaint()
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        except Exception as exc:
            # Never interfere with adding PostgreSQL layers if styles are inaccessible.
            from qgis.core import QgsMessageLog, Qgis
            QgsMessageLog.logMessage(
                'Style auto-load skipped for {}: {}'.format(layer.name(), exc),
                'GDB to PostgreSQL', Qgis.Warning
            )
        finally:
            self._busy.discard(lid)
